"""HTTP integration of the bounded manual identity reference panel."""
import json
from tests import test_memory_v4 as v4
class MemberContextHttpTests(v4.ConsoleTestBase):
 def test_recent_reference_requires_auth_and_stays_in_the_selected_group(self):
  self.assertEqual(self.request('/api/identity/member-context?group=1&user=100',token=False)[0],401)
  status,p,_=self.request('/api/identity/member-context?group=1&user=100');self.assertEqual(status,200);p=json.loads(p) if isinstance(p,bytes) else p
  self.assertLessEqual(len(p['data']['messages']),3)
  self.assertTrue(all(len(x['excerpt'])<=240 for x in p['data']['messages']))
  status,p,_=self.request('/api/identity/member-context?group=2&user=202');self.assertEqual(status,200);p=json.loads(p) if isinstance(p,bytes) else p
  self.assertFalse(any('私聊秘密' in x['excerpt'] for x in p['data']['messages']))
  self.assertEqual(self.request('/api/identity/member-context?group=2&user=100')[0],404)
 def test_private_and_extra_parameters_are_rejected(self):
  self.assertEqual(self.request('/api/identity/member-context?group=private:202&user=202')[0],400)
  self.assertEqual(self.request('/api/identity/member-context?group=1&user=100&path=anything')[0],400)
 def test_forwarded_loopback_port_is_supported_and_other_origins_are_rejected(self):
  import http.client
  forward='127.0.0.1:49123'
  self.assertEqual(self.request('/api/identity/member-context?group=1&user=100',headers={'Host':forward})[0],200)
  connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
  try:
   connection.request('POST','/api/identity/preview',body='{"action":"invalid"}',headers={'Host':forward,'Origin':'http://'+forward,'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
   response=connection.getresponse();self.assertEqual(response.status,400);self.assertEqual(json.loads(response.read())['error']['code'],'invalid_action')
  finally:connection.close()
  self.assertEqual(self.request('/api/identity/member-context?group=1&user=100',headers={'Host':forward,'Origin':'http://127.0.0.1:49124'})[0],403)
