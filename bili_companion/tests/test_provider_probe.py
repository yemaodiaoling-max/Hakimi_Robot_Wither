import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from bili_companion.cognition import provider_probe as probe


KEY='synthetic-provider-test-secret'


class Response(io.BytesIO):
    def __init__(self,url,value):
        super().__init__(json.dumps(value).encode())
        self.url=url

    def geturl(self):return self.url


class ProviderProbeTests(unittest.TestCase):
    def test_gemini_list_is_metadata_only_never_video_or_pricing_proof(self):
        data={'models':[{'name':'models/gemini-test','supportedGenerationMethods':['generateContent','untrusted-tool'],
                         'inputTokenLimit':10000,'outputTokenLimit':100,'description':KEY}]}
        with patch.object(probe,'_read',return_value=data) as read:
            result=probe.probe_models('https://provider.test',KEY,'gemini')
        self.assertEqual(read.call_args.args,('https://provider.test/v1beta/models',KEY,'gemini'))
        self.assertEqual(result['model_ids'],['gemini-test'])
        self.assertEqual(result['models'][0]['declared_methods'],['generateContent'])
        self.assertEqual(result['audio_video_capability'],'unverified')
        self.assertEqual(result['pricing_status'],'missing')
        self.assertFalse(result['activation_allowed'])
        self.assertFalse(result['budget_change_allowed'])
        self.assertNotIn(KEY,json.dumps(result))

    def test_openai_custom_base_prefix_and_auto_only_same_submitted_origin(self):
        with patch.object(probe,'_read',side_effect=[probe._ProbeError('not_found'),{'data':[{'id':'org/model-1'}]}]) as read:
            result=probe.probe_models('https://provider.test/custom',KEY)
        self.assertEqual(result['protocol'],'openai')
        self.assertTrue(all(call.args[0]=='https://provider.test/custom/models' for call in read.call_args_list))
        self.assertEqual(len(read.call_args_list),2)
        with patch.object(probe,'_read',return_value={'data':[{'id':'model-1'}]}) as read:
            result=probe.probe_models('https://provider.test/v1/chat/completions',KEY)
        self.assertEqual(read.call_args.args[0],'https://provider.test/v1/models')
        self.assertEqual(read.call_args.args[2],'openai')
        self.assertIn('音频',result['compatibility_note'])

    def test_invalid_urls_or_unconfirmed_http_send_no_requests(self):
        urls=['https://user:password@provider.test','https://provider.test?key=secret',
              'https://provider.test/#fragment','https://provider.test/%2e%2e/private',
              'file:///tmp/foo','https://provider.test\\@evil.test','http://provider.test']
        with patch.object(probe,'_read') as read:
            for url in urls:
                with self.subTest(url=url):
                    self.assertEqual(probe.probe_models(url,KEY)['state'],'probe_failed')
            read.assert_not_called()
        with patch.object(probe,'_read',return_value={'data':[{'id':'model'}]}) as read:
            self.assertEqual(probe.probe_models('http://127.0.0.1:1234/v1',KEY,'openai',allow_insecure_http=True)['state'],'models_ready')
        self.assertTrue(read.call_args.args[0].startswith('http://127.0.0.1:1234/'))

    def test_key_echo_removed_and_limits_do_not_publish_descriptions(self):
        payload={'data':[{'id':KEY},{'id':'model-'+KEY},{'id':'normal-model','description':KEY,'owner':KEY}]}
        with patch.object(probe,'_read',return_value=payload):
            result=probe.probe_models('https://provider.test/v1',KEY)
        self.assertEqual(result['model_ids'],['normal-model'])
        self.assertNotIn(KEY,json.dumps(result))
        payload={'models':[{'name':f'models/m-{i}'} for i in range(201)],'nextPageToken':KEY}
        with patch.object(probe,'_read',return_value=payload):
            result=probe.probe_models('https://provider.test',KEY,'gemini')
        self.assertEqual(len(result['models']),200)
        self.assertTrue(result['truncated'])
        self.assertNotIn(KEY,json.dumps(result))

    def test_request_auth_header_not_query_and_no_body_or_generation(self):
        url='https://provider.test/v1/models'
        with patch.object(probe,'build_opener') as build:
            build.return_value.open.return_value=Response(url,{'data':[{'id':'model'}]})
            result=probe.probe_models('https://provider.test/v1',KEY,'openai')
        request=build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url,url)
        self.assertEqual(request.get_method(),'GET')
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header('Authorization'),'Bearer '+KEY)
        self.assertNotIn(KEY,request.full_url)
        self.assertTrue(any(isinstance(h,probe._NoRedirect) for h in build.call_args.args))
        self.assertEqual(result['state'],'models_ready')

    def test_redirect_rejected_even_to_same_host_and_error_body_unread(self):
        with self.assertRaises(probe._ProbeError):
            probe._NoRedirect().redirect_request(None,None,302,'',{},'https://provider.test/new')
        body=io.BytesIO(KEY.encode())
        with patch.object(probe,'build_opener') as build:
            build.return_value.open.side_effect=HTTPError('https://provider.test/v1/models',401,KEY,{},body)
            result=probe.probe_models('https://provider.test/v1',KEY,'openai')
        self.assertEqual(result['error'],'auth_rejected')
        self.assertNotIn(KEY,json.dumps(result))
        self.assertTrue(body.closed)

    def test_malformed_and_oversized_responses_fail_closed(self):
        url='https://provider.test/v1/models'
        for raw in (b'not JSON',b'[]',b' '* (probe.MAX_BYTES+1)):
            class BadResponse(io.BytesIO):
                def geturl(self):return url
            with self.subTest(length=len(raw)),patch.object(probe,'build_opener') as build:
                build.return_value.open.return_value=BadResponse(raw)
                result=probe.probe_models('https://provider.test/v1',KEY,'openai')
                self.assertEqual(result['state'],'probe_failed')
                self.assertEqual(result['models'],[])


if __name__=='__main__':unittest.main()
