import copy
import json
from pathlib import Path
import tempfile
import unittest

from hakimi.core.self_reply_context import with_recent_own_replies


class SelfReplyContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base/'state').mkdir()
        self.path = self.base/'state/sent_messages.jsonl'
        self.data = {'session_key': 'group:123'}
        self.messages = [dict(session_key='group:123', user_id='u', message_id=str(i),
                              received_at=10000+i, text='input') for i in range(10)]

    def write(self, rows, mode='w'):
        with self.path.open(mode, encoding='utf-8') as stream:
            for row in rows:
                stream.write(json.dumps(row)+'\n')

    def receipt(self, mid, at=9999, **kw):
        return dict(session_key='group:123', message_id=mid, at=at, text='delivered '+str(mid), **kw)

    def context(self):
        return with_recent_own_replies(self.messages, self.data, self.base, 11000)

    def test_keeps_ten_inputs_and_restores_recent_own_replies_in_order(self):
        self.write([self.receipt('sent')])
        before = copy.deepcopy(self.messages)
        result = self.context()
        self.assertEqual(len(result), 11)
        self.assertEqual(result[0]['user_id'], 'SELF')
        self.assertEqual(result[0]['text'], 'delivered sent')
        self.assertEqual(self.messages, before)

    def test_session_time_and_empty_delivery_are_filtered(self):
        rows = [self.receipt('ok'), self.receipt('future', 10010),
                self.receipt('old', 6000), self.receipt('bad', float('nan'))]
        rows += [dict(self.receipt('other'), session_key='private:123'),
                 dict(self.receipt('empty'), text='')]
        self.write(rows)
        self.assertEqual([r['message_id'] for r in self.context() if r['user_id']=='SELF'], ['ok'])

    def test_duplicate_receipt_and_live_self_row_are_one_message(self):
        self.write([self.receipt('same'), self.receipt('same')])
        self.messages.insert(0, dict(session_key='group:123', user_id='SELF', message_id='same',
                                     received_at=9999, text='live'))
        self.assertEqual(sum(r['user_id']=='SELF' for r in self.context()), 1)

    def test_capped_receipt_does_not_truncate_complete_live_self_text(self):
        complete = '完整正文'*500 + 'UNTRUNCATED_END'
        self.write([dict(self.receipt('same'), text=complete[:1500])])
        self.messages.insert(0, dict(session_key='group:123', user_id='SELF', message_id='same',
                                     received_at=9999, text=complete))
        own = [r for r in self.context() if r['user_id']=='SELF']
        self.assertEqual(len(own),1)
        self.assertEqual(own[0]['text'],complete)

    def test_live_media_cq_is_replaced_without_losing_existing_caption(self):
        self.write([dict(self.receipt('image'), text='', media={'kind':'sticker','asset':'cat.jpg'})])
        self.messages.insert(0, dict(session_key='group:123', user_id='SELF', message_id='image',
                                     received_at=9999, text='完整图注 [CQ:image,file=file:///private/cat.jpg,sub_type=1]'))
        own = [r for r in self.context() if r['user_id']=='SELF']
        self.assertEqual(len(own),1)
        self.assertIn('完整图注',own[0]['text'])
        self.assertIn('我发出的收藏表情',own[0]['text'])
        self.assertNotIn('[CQ:',own[0]['text'])
        self.assertNotIn('/private/',own[0]['text'])

    def test_multiple_selected_self_rows_without_ids_are_not_collapsed(self):
        self.messages[:0] = [dict(user_id='SELF', received_at=9997, text='第一条历史'),
                            dict(user_id='SELF', received_at=9998, text='第二条历史')]
        self.write([self.receipt('recent')])
        own = [r['text'] for r in self.context() if r['user_id']=='SELF']
        self.assertEqual(own,['第一条历史','第二条历史','delivered recent'])

    def test_five_reply_bound_keeps_explicitly_referenced_older_reply(self):
        self.write([self.receipt('s'+str(i), 9900+i) for i in range(8)])
        self.messages[-1]['reply_to'] = 's0'
        own = [r['message_id'] for r in self.context() if r['user_id']=='SELF']
        self.assertEqual(own, ['s0','s3','s4','s5','s6','s7'])

    def test_cached_tail_observes_appended_confirmation(self):
        self.write([self.receipt('one')])
        self.assertEqual(len(self.context()), 11)
        self.write([self.receipt('two', 10001)], 'a')
        self.assertEqual(len(self.context()), 12)

    def test_no_timestamp_keeps_legacy_view_and_created_at_bounds_replay(self):
        self.write([self.receipt('one')])
        self.messages = [dict(user_id='u', message_id='legacy', text='input')]
        self.assertEqual(self.context(), self.messages)
        self.data['created_at'] = 9998
        self.assertEqual(self.context(), self.messages)
        self.data['created_at'] = 10000
        self.assertEqual(len(self.context()), 2)

    def test_pending_outbox_is_not_delivery_evidence(self):
        (self.base/'state/outbox.jsonl').write_text(json.dumps(self.receipt('draft'))+'\n')
        self.assertEqual(self.context(), self.messages)

    def test_selected_older_self_history_survives_receipt_age_bound(self):
        self.messages.insert(0, dict(session_key='group:123', user_id='SELF', message_id='old-selected',
                                     received_at=1, text='legitimate selected history'))
        self.write([self.receipt('recent')])
        self.assertEqual([r['message_id'] for r in self.context() if r['user_id']=='SELF'],
                         ['old-selected', 'recent'])

    def test_shared_model_context_and_both_final_prompts_contain_confirmed_reply(self):
        from hakimi.prompt.prompt_context import build_context
        from hakimi.transport.official_waiter import render_official_prompt
        from hakimi.models.fallback_prompt import fallback_prompt
        self.write([dict(self.receipt('confirmed'), text='CONFIRMED_PREVIOUS_REPLY')])
        self.messages[-1]['reply_to'] = 'confirmed'
        payload = dict(self.data, recent_context_messages=self.messages,
                       listening_window={'selected_message_ids': [str(i) for i in range(10)]})
        context = build_context(payload, self.base, 11000)
        rows = context['recent_context_messages_untrusted']
        self.assertEqual(sum(r['user_id'] != 'SELF' for r in rows), 10)
        self.assertEqual(sum(r['user_id'] == 'SELF' for r in rows), 1)
        self.assertEqual(context['reply_target']['message_id'], '9')
        self.assertEqual(context['reply_relationships'], {'9': 'confirmed'})
        (self.base/'style_guide.md').write_text('自然接话', encoding='utf-8')
        primary = render_official_prompt(payload, self.base, {'token': 'test-only'})
        fallback = fallback_prompt(payload, self.base)
        for prompt in (primary, fallback):
            self.assertIn('CONFIRMED_PREVIOUS_REPLY', prompt)
            self.assertIn('SELF', prompt)
            self.assertIn('reply_relationships', prompt)
            self.assertIn('confirmed', prompt)


if __name__ == '__main__':
    unittest.main()
