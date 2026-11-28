import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from hakimi.bridge import conversation_judge as judge
from hakimi.core.context_window import select_context


def row(mid,**extra):
    return dict(message_id=str(mid),user_id='alice',session_key='group:10001',received_at=float(mid),text='上下文'+str(mid),**extra)


class MentionSelectionTests(unittest.TestCase):
    def test_wider_input_keeps_focus_and_caps_final_window(self):
        rows=[row(i) for i in range(1,81)];seen=[]
        def runner(task,base):
            data=json.loads(task);data['decoded']=judge.decode_messages(data);seen.append(data)
            return {'selected_message_ids':[str(i) for i in range(1,81)]}
        ids=judge.select_messages(rows,'前次回复',Path('.'),runner,focus_ids=['80'],limit=12)
        self.assertEqual(len(seen[0]['decoded']),80)
        self.assertEqual(seen[0]['focus_message_ids'],['80'])
        self.assertEqual(len(ids),12);self.assertIn('80',ids)
        selected=[r for r in rows if r['message_id'] in ids]
        final,target=select_context({'session_key':'group:10001','recent_context_messages':selected,'pending_explicit_question_ids':['80']})
        self.assertEqual(len(final),12);self.assertEqual(target['message_id'],'80')

    def test_short_selection_is_not_padded(self):
        rows=[row(i) for i in range(1,31)]
        ids=judge.select_messages(rows,'',Path('.'),lambda *_:{'selected_message_ids':['4']},focus_ids=['30'],limit=12)
        final,_=select_context({'session_key':'group:10001','recent_context_messages':[r for r in rows if r['message_id'] in ids]})
        self.assertEqual([r['message_id'] for r in final],['4','30'])

    def test_own_history_and_image_question_are_candidates(self):
        rows=[dict(row(1),user_id='SELF'),dict(row(2),text='',image_paths=['example.jpg'])]
        seen=[]
        def runner(task,base):seen.append(dict(json.loads(task)));seen[-1]['decoded']=judge.decode_messages(seen[-1]);return {'selected_message_ids':[]}
        ids=judge.select_messages(rows,'',Path('.'),runner,focus_ids=['2'],limit=12)
        self.assertEqual(ids,['2'])
        self.assertEqual(len(seen[0]['decoded']),2)
        self.assertEqual(seen[0]['decoded'][-1]['text'],'[图片]')


class MentionQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):judge._mention_task=None

    async def test_background_review_is_serial_and_reuses_result(self):
        entered=threading.Event();release=threading.Event();calls=[]
        def select(rows,*args,**kwargs):
            calls.append(rows);entered.set();release.wait(2);return kwargs['focus_ids']
        one={};two={};rows=[row(1),row(2)]
        with patch.object(judge,'select_messages',side_effect=select):
            self.assertIsNone(judge.mention_context(one,rows,[rows[-1]],Path('.')))
            await asyncio.to_thread(entered.wait,1)
            self.assertIsNone(judge.mention_context(two,rows,[rows[-1]],Path('.')))
            self.assertNotIn('mention_review',two)
            release.set();await one['mention_review']['task']
            result=judge.mention_context(one,rows,[rows[-1]],Path('.'))
            self.assertEqual([r['message_id'] for r in result],['2']);self.assertEqual(len(calls),1)
            self.assertEqual(one['mention_review']['through'],2)

    async def test_new_at_during_review_is_rechecked(self):
        rows=[row(1),row(2),row(3)];entry={};calls=[]
        def select(rows,*args,**kwargs):calls.append(kwargs['focus_ids']);return kwargs['focus_ids']
        with patch.object(judge,'select_messages',side_effect=select):
            judge.mention_context(entry,rows[:2],[rows[1]],Path('.'));await entry['mention_review']['task']
            self.assertIsNone(judge.mention_context(entry,rows,[rows[1],rows[2]],Path('.')))
            await entry['mention_review']['task']
            result=judge.mention_context(entry,rows,[rows[1],rows[2]],Path('.'))
            self.assertEqual([r['message_id'] for r in result],['2','3'])
            self.assertEqual(calls,[['2'],['2','3']])

    async def test_failed_review_keeps_explicit_question(self):
        rows=[row(1),row(2)];entry={}
        with tempfile.TemporaryDirectory() as d,patch.object(judge,'select_messages',side_effect=RuntimeError('judge_unavailable')):
            judge.mention_context(entry,rows,[rows[1]],Path(d))
            try:await entry['mention_review']['task']
            except RuntimeError:pass
            with patch('hakimi.features.codex_help.report_fault'):
                result=judge.mention_context(entry,rows,[rows[1]],Path(d))
            self.assertEqual([r['message_id'] for r in result],['2'])


if __name__=='__main__':unittest.main()
