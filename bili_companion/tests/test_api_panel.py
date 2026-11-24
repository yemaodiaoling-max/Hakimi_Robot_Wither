import http.client
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest

from bili_companion.panel.server import PanelServer,PAGE
from bili_companion.panel.vault import LocalVault,validate_url


class PanelTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'state/test_tmp';root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.vault=LocalVault(self.temp.name,memory_only=True)
        self.received=[]
        def probe(url,key,protocol,**kwargs):
            self.received.append((url,key,protocol,kwargs))
            return dict(state='models_ready',protocol='gemini',model_ids=['gemini-test'])
        self.server=PanelServer(0,self.vault,probe)
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
        self.host='127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.worker.join(3);self.temp.cleanup()

    def request(self,path='/',*,post=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        values={'Host':self.host}
        if post is not None:values.update({'Content-Type':'application/json','Origin':self.server.origin,'X-Panel-Token':self.server.token})
        values.update(headers or {})
        connection.request('GET' if post is None else 'POST',path,body=json.dumps(post) if post is not None else None,headers=values)
        response=connection.getresponse();result=(response.status,response.read().decode(),dict(response.getheaders()))
        connection.close();return result

    def test_panel_has_password_and_no_persistence_or_raw_key_echo(self):
        status,html,headers=self.request()
        self.assertEqual(status,200);self.assertIn('type="password"',html)
        self.assertNotIn('localStorage',html);self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertNotIn("'\n检测结果",PAGE)
        self.assertIn(r"'\n检测结果",PAGE)
        self.assertIn('function refreshStatus()',PAGE)
        self.vault.save_submission('https://example.com','private-key-for-test')
        self.assertNotIn('private-key-for-test',self.request('/status')[1])
        self.assertNotIn('private-key-for-test',self.request('/')[1])

    def test_host_origin_and_csrf_are_required(self):
        body=dict(base_url='https://example.com',api_key='private-test-key')
        self.assertEqual(self.request(headers={'Host':'attacker.example'})[0],403)
        self.assertEqual(self.request('/submit',post=body,headers={'Origin':'https://attacker.example'})[0],403)
        self.assertEqual(self.request('/submit',post=body,headers={'X-Panel-Token':'bad'})[0],403)
        self.assertEqual(self.request(headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertIsNone(self.vault.read_submission())

    def test_submit_probes_and_status_has_no_secret_or_live_change(self):
        code,body,_=self.request('/submit',post=dict(base_url='  https://example.com/v1  ',api_key='  private-test-key  '))
        self.assertEqual(code,202);self.assertNotIn('private-test-key',body)
        for _ in range(100):
            if self.vault.public_status()['phase']=='models_ready':break
            threading.Event().wait(.01)
        status=self.vault.public_status()
        self.assertEqual(status['phase'],'models_ready');self.assertEqual(status['budget_usd'],10)
        self.assertFalse(status['live_config_changed']);self.assertEqual(self.received[0][1],'private-test-key')
        self.assertEqual(self.received[0][0],'https://example.com/v1')
        self.assertEqual(status['model_ids'],['gemini-test'])
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])

    def test_remote_http_needs_checkbox_but_loopback_http_is_supported(self):
        body=dict(base_url='http://example.com/v1',api_key='test-key')
        self.assertEqual(self.request('/submit',post=body)[0],400)
        body['insecure_http_confirmed']=True
        self.assertEqual(self.request('/submit',post=body)[0],202)
        self.assertEqual(self.request('/submit',post=dict(base_url='http://127.0.0.1:9000',api_key='test-key'))[0],202)

    def test_url_rejects_credentials_query_and_fragments(self):
        for url in ('https://user:password@example.com','https://example.com?key=x','https://example.com#secret','file:///tmp/key'):
            with self.assertRaises(ValueError):validate_url(url)

    def test_stale_probe_cannot_update_new_submission_and_status_rejects_secret(self):
        old=self.vault.save_submission('https://example.com','old-test-key')
        current=self.vault.save_submission('https://example.com','new-test-key')
        self.assertFalse(self.vault.write_status(old,'models_ready',model_ids=['old-model']))
        with self.assertRaises(ValueError):self.vault.write_status(current,'models_ready',model_ids=['new-test-key'])

    def test_stale_page_after_server_restart_refreshes_token_without_losing_csrf(self):
        old_page=self.request()[1]
        old_token=re.search("token='([^']+)'",old_page).group(1)
        port=self.server.server_port;probe=self.server.probe_function
        self.server.shutdown();self.server.server_close();self.worker.join(3)
        self.server=PanelServer(port,self.vault,probe)
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
        body=dict(base_url='https://example.com',api_key='synthetic-retained-key')
        self.assertEqual(self.request('/submit',post=body,headers={'X-Panel-Token':old_token})[0],403)
        code,text,headers=self.request('/session-token')
        fresh=json.loads(text)['token']
        self.assertEqual(code,200);self.assertNotEqual(fresh,old_token)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(self.request('/submit',post=body,headers={'X-Panel-Token':fresh})[0],202)
        self.assertEqual(self.request('/session-token',headers={'Host':'attacker.example'})[0],403)
        self.assertEqual(self.request('/session-token',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertIn("fetch('/session-token'",PAGE)
        self.assertEqual(PAGE.count("key.value=''"),1)
        self.assertIn('输入已保留',PAGE)

    def test_live_configuration_change_is_internal_and_requires_active(self):
        identity=self.vault.save_submission('https://example.com','status-test-key')
        with self.assertRaises(ValueError):self.vault.write_status(identity,'verified',live_config_changed=True)
        with self.assertRaises(ValueError):self.vault.write_status(identity,'active',live_config_changed=1)
        self.assertTrue(self.vault.write_status(identity,'active',live_config_changed=True,budget_usd=40))
        status=json.loads(self.request('/status')[1])
        self.assertTrue(status['live_config_changed']);self.assertEqual(status['budget_usd'],40)
        self.vault.write_status(identity,'testing')
        self.assertFalse(self.vault.public_status()['live_config_changed'])
        self.assertEqual(self.request('/submit',post=dict(base_url='https://example.com',api_key='status-test-key',
            live_config_changed=True))[0],400)

    def test_activity_is_read_only_whitelisted_and_guarded(self):
        path=Path(self.temp.name)/'activity.json'
        self.server.activity_file=path
        self.assertFalse(json.loads(self.request('/activity-data')[1])['available'])
        path.write_text(json.dumps(dict(schema_version=1,exported_at=1793500000,api_key='secret-test',
            video_history=[dict(bvid='BV1234567890',observations=1,status='sampled',summary='<script>test</script>',
                updated_at=1793500000,coverage=[dict(start=1,end=4,modality='audio',media_url='secret-url')],private='secret-test')],
            video_comment_attempts=[dict(bvid='BV1234567890',text='hello',state='unknown',at=1793500000,account='secret-test')])),encoding='utf-8')
        code,body,_=self.request('/activity-data')
        self.assertEqual(code,200);self.assertNotIn('secret-test',body);self.assertNotIn('secret-url',body)
        data=json.loads(body);self.assertEqual(data['exported_at'],1793500000)
        self.assertEqual(data['video_history'][0]['coverage'],[dict(start=1,end=4,modality='audio')])
        page=self.request('/activity')[1]
        self.assertIn('结果未知',page);self.assertIn('textContent',page);self.assertNotIn('innerHTML',page)
        self.assertIn('/activity',self.request('/')[1])
        for endpoint in ('/activity','/activity-data'):
            self.assertEqual(self.request(endpoint,headers={'Host':'attacker.example'})[0],403)
            self.assertEqual(self.request(endpoint,headers={'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertEqual(self.request('/activity-data',post={})[0],404)
        path.write_text(json.dumps(dict(schema_version=1,video_history={},video_comment_attempts=[])),encoding='utf-8')
        self.assertEqual(self.request('/activity-data')[0],503)
        path.write_text('{broken',encoding='utf-8')
        self.assertEqual(self.request('/activity-data')[0],503)


@unittest.skipUnless(os.name=='nt','Windows DPAPI only')
class DPAPITests(unittest.TestCase):
    def test_user_bound_roundtrip_on_disk_and_public_status(self):
        root=Path(__file__).resolve().parents[1]/'state/test_tmp';root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            vault=LocalVault(directory)
            identity=vault.save_submission('https://example.com/v1','DPAPI-test-key')
            raw=(Path(directory)/'submission.dpapi').read_bytes()
            self.assertNotIn(b'DPAPI-test-key',raw)
            reopened=LocalVault(directory)
            self.assertEqual(reopened.read_submission()['api_key'],'DPAPI-test-key')
            self.assertEqual(reopened.public_status()['submission_id'],identity)
            self.assertNotIn('DPAPI-test-key',json.dumps(reopened.public_status()))


if __name__=='__main__':unittest.main()
