import json
import io
from urllib.error import HTTPError
import unittest
from unittest.mock import patch
from bili_companion.cognition.openai_provider import OpenAICompatibleClient
from bili_companion.cognition.gateway import CognitionUnavailable


class OpenAIProviderTests(unittest.TestCase):
    def client(self,**kw):
        return OpenAICompatibleClient('https://provider.test/v1',api_key='synthetic-test-key',
                                      input_token_upper_bound=8192,**kw)

    def payload(self):
        return {'systemInstruction':{'parts':[{'text':'trusted persona'}]},
                'contents':[{'parts':[{'text':'untrusted observations'}]}],
                'generationConfig':{'maxOutputTokens':64,'responseMimeType':'application/json'}}

    def test_explicit_upper_is_not_claimed_official_count_and_output_reasoning_limit(self):
        client=self.client(reasoning_effort='low')
        self.assertEqual(client.count_tokens('gemini-3.8-flash-high',self.payload()),8192)
        self.assertEqual(client.count_kind,'configured_upper_bound')
        data=client._prepare('gemini-3.8-flash-high',self.payload())
        self.assertEqual(data['max_completion_tokens'],64)
        self.assertEqual(data['reasoning_effort'],'low')
        self.assertEqual(data['messages'][0],{'role':'system','content':'trusted persona'})
        self.assertNotIn('tools',data)
        self.assertEqual(data['response_format'],{'type':'json_object'})

    def test_panel_cost_requires_attribution_and_zero_counter_progress(self):
        from decimal import Decimal
        snapshot=dict(requests=2,input_tokens=100,output_tokens=20,cost=Decimal(0),actual_cost=Decimal(0))
        response={'usageMetadata':dict(promptTokenCount=10,candidatesTokenCount=3,thoughtsTokenCount=2)}
        exact=dict(snapshot,requests=3,input_tokens=110,output_tokens=25,actual_cost=Decimal('.002'))
        self.assertEqual(self.client().panel_actual_cost(snapshot,exact,response),Decimal('.002'))
        self.assertIsNone(self.client().panel_actual_cost(snapshot,snapshot,response))
        concurrent=dict(snapshot,requests=4,input_tokens=130,output_tokens=40)
        self.assertEqual(self.client().panel_actual_cost(snapshot,concurrent,response),0)
        self.assertIsNone(self.client().panel_actual_cost(snapshot,dict(concurrent,actual_cost=Decimal('.01')),response))
        self.assertIsNone(self.client().panel_actual_cost(snapshot,dict(concurrent,input_tokens=105),response))
        self.assertIsNone(self.client().panel_actual_cost(snapshot,concurrent,{}))

    def test_panel_reads_bracket_single_generation_and_missing_usage_is_usable(self):
        class Reply(io.BytesIO):
            def geturl(self):return 'https://provider.test/v1/chat/completions'
        client=self.client(usage_panel_actual_cost=True)
        body={'model':'model','choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
        with patch.object(client,'usage_snapshot',side_effect=[None,None]) as snapshot,patch('bili_companion.cognition.openai_provider.build_opener') as opener:
            opener.return_value.open.return_value=Reply(json.dumps(body).encode())
            result=client.generate('model',self.payload())
            self.assertEqual(snapshot.call_count,2)
            self.assertEqual(opener.return_value.open.call_count,1)
            self.assertIsNone(result['_provider_actual_cost_usd'])
            self.assertEqual(result['_provider_response_metadata']['reported_model'],'model')
            self.assertEqual(result['candidates'][0]['finishReason'],'STOP')

    def test_payload_envelope_is_opt_in(self):
        client=self.client(payload_token_envelope=True)
        self.assertEqual(client.count_tokens('model',self.payload()),2048+len('trusted personauntrusted observations'.encode()))
        self.assertEqual(self.client().count_tokens('model',self.payload()),8192)

    def test_panel_snapshot_only_same_origin_get_and_strict_usd_model(self):
        class Reply(io.BytesIO):
            def geturl(self):return 'https://provider.test/v1/usage'
        value=dict(unit='USD',model_stats=[dict(model='model',requests=1,input_tokens=10,output_tokens=5,cost=0,actual_cost=0)])
        with patch('bili_companion.cognition.openai_provider.build_opener') as opener:
            opener.return_value.open.return_value=Reply(json.dumps(value).encode())
            snapshot=self.client().usage_snapshot('model')
            self.assertEqual(snapshot['requests'],1)
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(),'GET')
            self.assertEqual(request.full_url,'https://provider.test/v1/usage')

    def test_usage_reasoning_is_not_double_billed_and_length_not_success(self):
        normalized=self.client().normalize({'usage':{'prompt_tokens':100,'completion_tokens':30,
            'completion_tokens_details':{'reasoning_tokens':20},'total_tokens':130},
            'choices':[{'finish_reason':'length','message':{'content':'truncated'}}]})
        usage=normalized['usageMetadata']
        self.assertEqual(usage['candidatesTokenCount'],10)
        self.assertEqual(usage['thoughtsTokenCount'],20)
        self.assertEqual(usage['totalTokenCount'],130)
        self.assertEqual(normalized['candidates'][0]['finishReason'],'MAX_TOKENS')
        with self.assertRaises(CognitionUnavailable):self.client().normalize({'choices':[]})

    def test_qq_can_accept_valid_content_without_valid_usage_while_autonomous_stays_strict(self):
        content={'choices':[{'finish_reason':'stop','message':{'content':'{"events":[]}'}}]}
        client=self.client(require_usage=False)
        for usage in (None,{}, {'completion_tokens_details':[]}, {'prompt_tokens':7,'completion_tokens':1,
                'completion_tokens_details':{'reasoning_tokens':5}}):
            value=dict(content,usage=usage)
            normalized=client.normalize(value,require_usage=client.require_usage)
            self.assertEqual(normalized['candidates'][0]['finishReason'],'STOP')
            self.assertNotIn('usageMetadata',normalized)
            with self.assertRaises(CognitionUnavailable):self.client().normalize(value)

    def test_provider_rejection_preserves_fixed_code_without_echoing_upstream_message(self):
        client=self.client()
        body=json.dumps({'code':'GROUP_DELETED','message':'rejected synthetic-test-key'}).encode()
        failure=HTTPError('https://provider.test/v1/chat/completions',403,'Forbidden',{},io.BytesIO(body))
        with patch('bili_companion.cognition.openai_provider.build_opener') as opener:
            opener.return_value.open.side_effect=failure
            with self.assertRaises(CognitionUnavailable) as rejected:client.generate('model',self.payload())
        self.assertEqual(rejected.exception.code,'provider_http_403')
        self.assertEqual(rejected.exception.http_status,403)
        self.assertEqual(rejected.exception.provider_code,'GROUP_DELETED')
        self.assertNotIn('synthetic-test-key',str(rejected.exception))

    def test_tools_and_untrusted_external_media_are_rejected(self):
        client=self.client()
        for part in ({'fileData':{'fileUri':'https://evil.test/media'}},{'functionCall':{'name':'shell'}}):
            payload=self.payload();payload['contents'][0]['parts']=[part]
            with self.assertRaises(CognitionUnavailable):client.count_tokens('model',payload)
        payload=self.payload();payload['tools']=[{'anything':'unsafe'}]
        with self.assertRaises(CognitionUnavailable):client.count_tokens('model',payload)

    def test_conversion_cached_for_budget_preflight_and_generate(self):
        payload=self.payload();payload['contents'][0]['parts'].insert(0,{'inlineData':{'mimeType':'video/mp4','data':'base64'}})
        client=self.client()
        media=[{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,frame','detail':'low'}},
               {'type':'input_audio','input_audio':{'format':'wav','data':'audio'}}]
        with patch.object(client,'_media',return_value=(media,1000)) as convert:
            client.count_tokens('model',payload)
            prepared=client._prepare('model',payload)
        convert.assert_called_once()
        self.assertEqual([p['type'] for p in prepared['messages'][1]['content']],['image_url','input_audio','text'])

    def test_hard_input_envelope_rejects_before_post(self):
        client=self.client()
        payload=self.payload();payload['contents'][0]['parts'][0]['text']='x'*1024*1024
        with patch('bili_companion.cognition.openai_provider.build_opener') as network:
            with self.assertRaises(CognitionUnavailable):client.generate('model',payload)
            network.assert_not_called()
        payload=self.payload();payload['contents'][0]['parts']=[{'inlineData':{'mimeType':'video/mp4','data':'test'}}]
        with patch.object(client,'_media',return_value=([],10000)):
            with self.assertRaises(CognitionUnavailable):client.count_tokens('model',payload)

    def test_only_complete_valid_json_fence_is_unwrapped(self):
        base={'usage':{'prompt_tokens':1,'completion_tokens':2},'choices':[{'finish_reason':'stop',
              'message':{'content':'```json\n{"ok":true}\n```'}}]}
        result=self.client().normalize(base)
        self.assertEqual(result['candidates'][0]['content']['parts'][0]['text'],'{"ok":true}')
        base['choices'][0]['message']['content']='extra ```json\n{"ok":true}\n```'
        self.assertTrue(self.client().normalize(base)['candidates'][0]['content']['parts'][0]['text'].startswith('extra'))

    def test_empty_message_wrapper_cannot_bypass_token_envelope(self):
        client=self.client()
        for payload in (
            {**self.payload(),'contents':[{'parts':[{'text':''}]}]*100000},
            {**self.payload(),'contents':[{'parts':[{'text':''}]*33}]},
            {**self.payload(),'systemInstruction':{'parts':[{'text':''}]*9}}):
            with self.subTest(),patch('bili_companion.cognition.openai_provider.build_opener') as network:
                with self.assertRaises(CognitionUnavailable):client.generate('model',payload)
                network.assert_not_called()

    def test_constructor_requires_safe_transport_and_bounded_input(self):
        with self.assertRaises(Exception):OpenAICompatibleClient('http://provider.test/v1',api_key='x',input_token_upper_bound=10)
        with self.assertRaises(ValueError):self.client(reasoning_effort='arbitrary')


if __name__=='__main__':unittest.main()
