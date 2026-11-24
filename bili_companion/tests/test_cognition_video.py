import json
import sqlite3
from unittest.mock import patch
import unittest
import test_cognition_live as helpers
from bili_companion.domain import Scope
from bili_companion.cognition.video import history_context
from bili_companion.cognition.live import CognitionUnavailable


def video_reply(*, interest='high', action='continue', confidence=.9, speak=True, indices=None):
    value = helpers.observed()
    value['events'][0]['confidence']=confidence
    value['video_feedback'] = dict(interest=interest,next_action=action,summary='本段有钢琴音乐。',
        evidence_indices=[0],comment=dict(speak=speak,text='只听这一小段，钢琴声还挺抓耳。' if speak else '',
                                          evidence_indices=[0] if indices is None else indices))
    return helpers.response(value)


class VideoCognitionTests(unittest.TestCase):
    setUp = helpers.CognitionTests.setUp
    engine = helpers.CognitionTests.engine

    def video_scope(self):
        self.chunk.target_id='BV1234567890'
        return Scope('a','video',self.chunk.target_id)

    def test_selection_uses_real_history_and_candidate_allowlist_without_observation(self):
        history=[dict(bvid='BV0987654321',interest='high',summary='真实已观察的片段摘要'*30,
                      coverage=[dict(start=0,end=30,modality='audio')]) for _ in range(12)]
        engine,client=self.engine([helpers.response(dict(bvid='BV1234567890',reason='想试试这类音乐'))])
        result=engine.select_video([dict(bvid='BV1234567890',title='只作选片资料',url='https://untrusted.test')],
                                   recent_video_history=history)
        self.assertEqual(result.bvid,'BV1234567890')
        with engine.ledger.database.connect() as db:
            audit=db.execute('SELECT request_id,bvid,reason,qq_context_json FROM video_selection_audit').fetchone()
        self.assertEqual(audit['request_id'], result.request_ids[0])
        self.assertEqual(audit['reason'], result.reason)
        self.assertEqual(json.loads(audit['qq_context_json']), [])
        self.assertIn('video-selection-',result.request_ids[0])
        data=json.loads(client.requests[0][1]['contents'][0]['parts'][0]['text'])
        self.assertNotIn('url',data['candidates'][0])
        self.assertTrue(data['actual_video_history'])
        self.assertLessEqual(len(json.dumps(data['actual_video_history'],ensure_ascii=False,separators=(',',':'))),1200)
        self.assertEqual(self.memory.recall(self.video_scope(),'音乐'),[])
        engine,_=self.engine([helpers.response(dict(bvid='BV9999999999',reason='out of list'))])
        result=engine.select_video([dict(bvid='BV1234567890',title='title')])
        self.assertIsNone(result.bvid)
        self.assertEqual(result.reason,'invalid_selection')

    def test_same_paid_observation_has_interest_summary_and_grounded_comment(self):
        engine,client=self.engine([video_reply()])
        result=engine.observe(self.chunk,self.video_scope(),4,allow_decision=True,
                              video_context={'title':'这是未核实标题，不作为事实'})
        self.assertEqual(len(client.requests),1)
        self.assertEqual(result.interest,'high')
        self.assertEqual(result.next_action,'continue')
        self.assertEqual(result.summary_evidence_ids,(result.observation.evidence[0].id,))
        self.assertEqual(result.comment_status,'ready')
        self.assertEqual(result.comment_candidate.bvid,'BV1234567890')
        self.assertEqual(result.comment_candidate.watch_epoch,4)
        self.assertEqual(self.memory.recall(self.video_scope(),'钢琴'),[])

    def test_detailed_segment_reaches_contextual_video_notes(self):
        from dataclasses import asdict
        from bili_companion.cognition.qq_summary import feedback_data

        value = helpers.observed()
        events = [dict(value['events'][0], text=f'{i}：' + '可辨的台词和动作' * 17)
                  for i in range(10)]
        value['events'] = events
        summary = '按时间串联画面与声音。' * 30
        value['video_feedback'] = dict(interest='neutral', next_action='continue', summary=summary,
            evidence_indices=list(range(10)), comment=dict(speak=False, text='', evidence_indices=[]))
        engine, _ = self.engine([helpers.response(value)])
        seen = engine.observe(self.chunk, self.video_scope(), 1, allow_decision=False)
        report = dict(duration=10, coverage_status='complete', evidence=[asdict(e) for e in seen.observation.evidence],
                      summaries=[dict(start=0, end=10, text=seen.summary)])
        data, _ = feedback_data(report, {'question': '按顺序讲讲发生了什么？'})
        self.assertEqual(data['summaries'][0]['summary'], summary)
        self.assertEqual([e['text'] for e in data['observations']], [e['text'] for e in events])

    def test_comment_reason_enums_and_facts_survive_bad_summary(self):
        for kwargs,age,enabled,expected in (
            ({'speak':False},0,True,'silent'),({},0,False,'disabled'),
            ({'confidence':.7},0,True,'low_confidence'),({},400,True,'expired'),
            ({'indices':[99]},0,True,'invalid')):
            with self.subTest(expected=expected):
                self.chunk.received_at=self.now-age
                engine,_=self.engine([video_reply(**kwargs)])
                result=engine.observe(self.chunk,self.video_scope(),2,allow_decision=enabled)
                self.assertEqual(result.comment_status,expected)
                self.assertIsNone(result.comment_candidate)
                self.assertTrue(result.observation.evidence)
        bad=helpers.observed()
        bad['video_feedback']=dict(interest='low',next_action='skip',summary='无依据总结',
                                  evidence_indices=[99],comment=dict(speak=False,text='',evidence_indices=[]))
        engine,_=self.engine([helpers.response(bad)])
        result=engine.observe(self.chunk,self.video_scope(),2)
        self.assertEqual(result.status,'invalid_video_feedback')
        self.assertEqual(result.summary,'')
        self.assertTrue(result.observation.evidence)

    def test_unknown_selection_keeps_purpose_for_budget_pool(self):
        engine,_=self.engine([TimeoutError('lost response')])
        with self.assertRaises(CognitionUnavailable) as failure:
            engine.select_video([dict(bvid='BV1234567890',title='metadata')])
        self.assertIn('video-selection-',failure.exception.request_ids[0])
        self.assertIn('video-selection-',self.ledger.entries()[0]['id'])
        self.assertEqual(self.ledger.entries()[0]['state'],'unknown')
        self.assertEqual(history_context([dict(bvid='x',interest='high',summary='no evidence',coverage=[])]),[])



    def test_selection_audits_only_qq_context_actually_supplied(self):
        engine,client=self.engine([helpers.response(dict(bvid='BV1234567890',reason='想听音乐'))])
        record=dict(source='qq',source_id='qq:M7',revision=3,summary='共享音乐偏好',source_refs=[],coverage=[],attribution='hearsay',occurred_at=1,is_digest=True)
        with patch('bili_companion.cognition.video.preference_memories',return_value=[record]):
            result=engine.select_video([dict(bvid='BV1234567890',title='音乐')])
        actual=json.loads(client.requests[0][1]['contents'][0]['parts'][0]['text'])['bridge_memory_data']
        with engine.ledger.database.connect() as db:
            row=db.execute('SELECT qq_context_json FROM video_selection_audit WHERE request_id=?',(result.request_ids[0],)).fetchone()
        self.assertEqual(json.loads(row[0]),[{k:r[k] for k in ('source_id','revision')} for r in actual])
        self.assertNotIn('summary',row[0])

    def test_selection_survives_audit_database_failure(self):
        engine,client=self.engine([helpers.response(dict(bvid='BV1234567890',reason='想听音乐'))])
        original_call=engine._call
        def call_then_break_audit(*args,**kwargs):
            response=original_call(*args,**kwargs)
            engine.ledger.database.connect=lambda **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError('private path'))
            return response
        with patch.object(engine,'_call',side_effect=call_then_break_audit), self.assertLogs('bili_companion.cognition.video',level='WARNING') as logs:
            result=engine.select_video([dict(bvid='BV1234567890',title='音乐')])
        self.assertEqual(result.bvid,'BV1234567890')
        self.assertEqual(result.audit_status,'failed')
        self.assertEqual(len(client.requests),1)
        self.assertNotIn('private path',' '.join(logs.output))

if __name__=='__main__':unittest.main()
