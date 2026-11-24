"""Daily cards keep source direction/time and survive bounded raw-chat cleanup."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from hakimi.memory import daily_events, impression_events
from hakimi.memory.memory_store import MemoryStore


class DailyEventTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.index = MemoryStore(temporary.name)
        self.now = time.time()
        with self.index.db() as db:
            daily_events.initialize(db)

    def messages(self):
        for mid, author, text, reply, targets in (
                ('one', '101', '我回忆上周参加了家乡的音乐节，真开心', '', []),
                ('two', '102', '下次去音乐节记得叫上我，我们约好了', 'one', ['101'])):
            self.index.observe({'session_key': 'group:1', 'user_id': author, 'text': text,
                                'message_id': mid, 'received_at': self.now - 8 * 86400,
                                'reply_to': reply, 'direct_targets': targets, 'self_id': '999'})

    def test_card_preserves_direction_and_separates_observed_from_old_event(self):
        self.messages()
        self.index.prune(self.now)
        with self.index.db() as db:
            packets = daily_events.prepare(db, self.now, with_impressions=True)
            self.assertEqual(packets[1]['reply_author'], '101')
            self.assertEqual(packets[1]['mentions'], ['101'])
            self.assertEqual(packets[1]['reply_context']['text'], packets[0]['text'])
            self.assertEqual(packets[1]['self_id'], '999')
            items = daily_events.validate([{'source_ids': [p['id'] for p in packets],
                'text': '101回忆上周去了音乐节；102对101说下次一起去。', 'event_time': '上周'}], packets)
            daily_events.apply(self.index, db, packets, items, self.now)
            daily_events.apply(self.index, db, packets, items, self.now)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records WHERE channel='daily_events'").fetchone()[0], 1)
        self.index.prune(self.now + 1)
        with self.index.db() as db:
            self.assertTrue(all(r[0] == '' for r in db.execute("SELECT text FROM records WHERE kind='chat'")))
            card = db.execute('SELECT * FROM daily_event_cards').fetchone()
            self.assertEqual(card['observed_at'], self.now - 8 * 86400)
            self.assertEqual(card['recorded_at'], self.now)
            self.assertEqual(card['event_time'], '上周')
            sources = json.loads(card['sources'])
            self.assertEqual(sources[1]['message_id'], 'two')
            self.assertEqual(sources[1]['reply_author'], '101')
            self.assertNotIn('text', sources[0])
        class Encoder:
            model_id = 'daily-test'
            revision = '1'
            def embed(self, texts, **kwargs):
                return [[1.0, 0.5] for _ in texts]
        indexed = self.index.index_pending_embeddings(Encoder(), now=self.now + 1)
        self.assertEqual(indexed['indexed'], 1)
        with self.index.db() as db:
            self.assertGreater(db.execute('SELECT length(vector) FROM embeddings WHERE record_id=?',
                                          (card['record_id'],)).fetchone()[0], 0)

    def test_bad_source_and_invented_event_time_are_rejected(self):
        packet = {'id': 1, 'session': 'group:1', 'text': '我回忆上周参加了音乐节'}
        for item in (
                {'source_ids': [2], 'text': '伪造来源'},
                {'source_ids': [1], 'text': '旧事发生在今天', 'event_time': '今天'}):
            with self.assertRaises(ValueError):
                daily_events.validate([item], [packet])
        other = dict(packet, id=2, session='private:2')
        with self.assertRaises(ValueError):
            daily_events.validate([{'source_ids': [1, 2], 'text': '混入私聊'}], [packet, other])

    def test_classifier_keeps_raw_json_separate_from_positive_instructions(self):
        from hakimi.prompt.prompt_language import prompt as positive_prompt
        messages = [{'id': 1, 'session': 'group:1', 'author': '101',
                     'text': '我说“不能去”，对方说“不要忘记下次约定”。\n后来约好了。'}]
        def invoke(task, workspace):
            rendered = positive_prompt(task)
            payload = json.loads(rendered[rendered.rfind('\n') + 1:])
            self.assertEqual(payload['daily_messages'], messages)
            self.assertIn('旧事按原文明示时间理解', rendered)
            self.assertIn('转述和引用保留归属', rendered)
            self.assertIn('Return one JSON object directly', rendered)
            self.assertNotIn('action=reply', rendered)
            self.assertNotIn('text as JSON', rendered)
            self.assertIn('同一JSON对象的顶层字段', rendered)
            return {'items': [], 'daily_events': []}
        with patch('hakimi.memory.review_runtime.classify', side_effect=invoke) as runner, \
                patch('hakimi.ops.cloud_service.environment', return_value={'PATH': os.environ.get('PATH', '')}):
            for notes in ([], [{'id': 3, 'current_note': '以后不要忘记这次约定'}]):
                with self.subTest(notes=bool(notes)):
                    result = impression_events.classify(notes, self.index.base, daily=messages)
                    self.assertEqual(result, {'items': [], 'daily_events': []})
            self.assertEqual(runner.call_count, 2)

    def test_failure_and_unprocessed_candidate_cannot_block_cleanup_forever(self):
        self.messages()
        self.index.prune(self.now)
        with self.index.db() as db:
            packets = daily_events.prepare(db, self.now, with_impressions=True)
            for attempt in range(4):
                daily_events.failed(db, packets, self.now + attempt * 300)
        self.index.prune(self.now + 901)
        with self.index.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records WHERE kind='chat' AND text!=''").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM daily_event_cards').fetchone()[0], 0)

    def test_greetings_do_not_trigger_a_judge_batch(self):
        for number, text in enumerate(('谢谢', '哈哈哈哈哈哈哈哈', '[CQ:image,file=a]')):
            self.index.observe({'session_key': 'group:1', 'user_id': '101', 'text': text,
                                'message_id': str(number), 'received_at': self.now - 120})
        with self.index.db() as db:
            self.assertEqual(daily_events.prepare(db, self.now), [])
        for number in range(2):
            self.index.observe({'session_key': 'group:1', 'user_id': '101',
                                'text': '引用别人的话[CQ:at,qq=999]和音乐节经历' + str(number),
                                'message_id': 'quote' + str(number), 'received_at': self.now - 120,
                                'direct_targets': []})
        with self.index.db() as db:
            packets = daily_events.prepare(db, self.now)
            self.assertEqual(len(packets), 2)
            self.assertTrue(all(p['mentions'] == [] for p in packets))

    def test_manual_note_time_does_not_replace_its_source_message_time(self):
        self.messages()
        receipt = self.index.remember_manual({'kind': 'person', 'user_id': '101', 'key': '旧事',
                                              'note': '101说以前去过音乐节。'},
                                             session='group:1', provider='gemini', now=self.now,
                                             origin_message_id='one')
        result = self.index.read(receipt['id'], 'group:1', ['101'], now=self.now)
        self.assertEqual(result['recorded_at'], self.now)
        self.assertEqual(result['observed_at'], self.now - 8 * 86400)
        self.assertEqual(result['event_time'], '')

    def test_old_discussion_archive_limit_does_not_hide_daily_card(self):
        self.messages()
        with self.index.db() as db:
            packets = daily_events.prepare(db, self.now, with_impressions=True)
            items = daily_events.validate([{'source_ids': [packets[0]['id']],
                'text': '101回忆上周参加了音乐节。', 'event_time': '上周'}], packets)
            daily_events.apply(self.index, db, packets, items, self.now)
        for number in range(41):
            self.index.note({'kind': 'episode', 'session_key': 'group:1',
                             'key': 'topic-' + str(number), 'note': '一次具体的话题讨论' + str(number),
                             'created_at': self.now - 100 + number, 'provider': 'deterministic'})
        self.index.prune(self.now)
        with self.index.db() as db:
            self.assertEqual(db.execute("SELECT state FROM records WHERE channel='daily_events'").fetchone()[0], 'active')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records WHERE kind='episode' AND state='archived'").fetchone()[0], 1)



    def test_multi_author_drops_person_id_and_prepare_includes_nicknames(self):
        from hakimi.prompt.prompt_language import DEFENSIVE
        self.assertIsNone(DEFENSIVE.search(daily_events.INSTRUCTION))
        self.index.observe({'session_key': 'group:1', 'user_id': '101', 'nickname': '主人',
                            'text': '用GPT来提升你的写作吧，你和gpt差不多会写', 'message_id': 'm1',
                            'received_at': self.now - 120, 'self_id': '999'})
        self.index.observe({'session_key': 'group:1', 'user_id': '102', 'nickname': '群友388',
                            'text': '宝宝就免了，在DS里劝我用GPT想看我辞职', 'message_id': 'm2',
                            'received_at': self.now - 100, 'reply_to': 'm1', 'direct_targets': ['101'], 'self_id': '999'})
        with self.index.db() as db:
            packets = daily_events.prepare(db, self.now, with_impressions=True)
            self.assertEqual(packets[0]['author_name'], '主人')
            self.assertEqual(packets[1]['author_name'], '群友388')
            self.assertEqual(packets[1]['reply_author_name'], '主人')
            self.assertEqual(packets[1]['mention_names'], {'101': '主人'})
            items = daily_events.validate([{'source_ids': [packets[0]['id'], packets[1]['id']],
                'text': '主人发提示逗群友388；群友388回称宝宝就免了。', 'event_time': ''}], packets)
            daily_events.apply(self.index, db, packets, items, self.now)
            rec = db.execute("SELECT user_id FROM records WHERE channel='daily_events' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(rec[0], '')


if __name__ == '__main__':
    unittest.main()
