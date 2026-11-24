import copy,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.models.provider_health import ProviderHealth
from hakimi.models.vertex_session import VertexSession

class PrimaryProgressTests(unittest.TestCase):
    def test_real_tool_execution_protected_then_inactivity_can_take_over(self):
        with tempfile.TemporaryDirectory() as folder:
            now=[1000.];c=TurnCoordinator(folder,clock=lambda:now[0]);c.publish({'session_key':'group:1','trigger_id':'tool'})
            claim=c.claim_primary();hard=claim['deadline']
            self.assertEqual(c.snapshot()['primary_idle_deadline'],1060.)
            self.assertTrue(c.tool_activity(claim['token'],'video.watch',140))
            now[0]=1110.;self.assertIsNone(c.claim_fallback());self.assertNotIn('takeover_suspected_at',c.snapshot())
            self.assertFalse(c.tool_activity('invalid','fake',400))
            self.assertTrue(c.tool_activity(claim['token']))
            now[0]=1170.;self.assertIsNone(c.claim_fallback())
            now[0]=1179.;self.assertIsNone(c.claim_fallback())
            now[0]=1180.;self.assertEqual(c.claim_fallback()['takeover_reason'],'primary_stalled')
            self.assertEqual(hard,1420.)

    def test_real_stream_progress_cancels_suspicion_and_hard_deadline_still_bounds_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            now=[1000.];c=TurnCoordinator(folder,clock=lambda:now[0]);c.publish({'session_key':'group:1','trigger_id':'stream'})
            claim=c.claim_primary();now[0]=1060.;self.assertIsNone(c.claim_fallback())
            now[0]=1065.;self.assertTrue(c.record_progress(claim['token']));self.assertNotIn('takeover_suspected_at',c.snapshot())
            self.assertTrue(c.tool_activity(claim['token'],'web.search',9999))
            now[0]=1419.;self.assertIsNone(c.claim_fallback())
            now[0]=1420.;self.assertEqual(c.claim_fallback()['takeover_reason'],'primary_hard_timeout')

    def test_late_complete_primary_reply_restores_normal_wait_without_duplicate_send(self):
        with tempfile.TemporaryDirectory() as folder:
            now=[1000.];base=Path(folder);c=TurnCoordinator(base,clock=lambda:now[0])
            c.publish({'session_key':'group:1','trigger_id':'late'});claim=c.claim_primary()
            def transport(payload,timeout):
                c.report_failure(claim['token'],'model_error');self.assertIsNotNone(c.claim_fallback())
                return {'candidates':[{'finishReason':'STOP','content':{'parts':[{'functionCall':{'name':'reply.send','args':{'text':'迟到回复'}}}]}}]}
            session=VertexSession([],base,{},transport=transport,image_loader=lambda claim:[],tool_runner=Mock())
            with patch('vertex_session.time.time',side_effect=lambda:now[0]):
                with self.assertRaisesRegex(RuntimeError,'ownership_lost'):session.generate('fixture',c,claim)
            session.tool_runner.assert_not_called()
            self.assertEqual(c.snapshot()['provider'],'dsh')
            self.assertEqual(ProviderHealth(base,clock=lambda:now[0]).snapshot()['streak'],0)
            self.assertFalse((base/'state/outbox.jsonl').exists())

class StreamTests(unittest.TestCase):
    def request(self,events):
        payload=b': keepalive\n\n'+b''.join(b'data: '+json.dumps(event).encode()+b'\n\n' for event in events)
        response=io.BytesIO(payload);response.headers={'X-Gateway-Request-Id':'test-id'}
        opener=Mock();opener.open.return_value=response
        env={'AGY_LLM_GATEWAY_URL':'https://localhost','AGY_LLM_GATEWAY_MODELS':'gemini-test','AGY_LLM_GATEWAY_CA_CERT':'unused','AGY_LLM_GATEWAY_API_KEY':'fixture-only'}
        session=VertexSession([],Path.cwd(),env);session._progress=Mock(return_value=True)
        with patch('vertex_session.build_opener',return_value=opener),patch('vertex_session.ssl.create_default_context'):
            result=session._request({'contents':[]},400)
        return session,result,opener

    def test_stream_preserves_signatures_and_complete_call_and_counts_only_real_parts(self):
        call={'functionCall':{'name':'memory.search','args':{'query':'fixture'}},'thoughtSignature':'sig1'}
        signature={'text':'','thoughtSignature':'sig2'}
        events=[{'candidates':[{'content':{'parts':[call]}}],'usageMetadata':{'totalTokenCount':10}},
                {'candidates':[{'content':{'parts':[signature]},'finishReason':'STOP'}],'usageMetadata':{'totalTokenCount':12}}]
        session,result,opener=self.request(events)
        self.assertEqual(result['candidates'][0]['content']['parts'],[call,signature])
        self.assertEqual(result['usageMetadata']['totalTokenCount'],12)
        self.assertEqual(session._progress.call_count,1)
        self.assertIn(':streamGenerateContent?alt=sse',opener.open.call_args.args[0].full_url)
        self.assertEqual(opener.open.call_args.kwargs['timeout'],90)

    def test_keepalives_and_usage_events_do_not_become_progress(self):
        session,result,_=self.request([{'usageMetadata':{'totalTokenCount':1}},
                                      {'candidates':[{'finishReason':'STOP','content':{'parts':[]}}]}])
        session._progress.assert_not_called()

    def test_truncated_stream_is_never_submitted(self):
        with self.assertRaisesRegex(RuntimeError,'incomplete_stream'):
            self.request([{'candidates':[{'content':{'parts':[{'text':'partial'}]}}]}])

if __name__=='__main__':unittest.main()
