"""Secondary-call payloads (context judge, note reviewer, reply reviewer) stay lossless but compact."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hakimi.bridge import conversation_judge as judge
from hakimi.curation import impression_events, post_reply_review as review


def legacy_judge_wire(batch):
    """The pre-compaction message encoding (one dict with every identity key per row)."""
    aliases = {row['message_id']: str(i + 1) for i, row in enumerate(batch)}
    out = []
    for row in batch:
        item = {key: row[key] for key in ('user_id', 'name', 'text', 'audience') if row.get(key)}
        item['message_id'] = aliases[row['message_id']]
        if row.get('reply_to'):item['reply_to'] = aliases.get(row['reply_to'], row['reply_to'])
        if row.get('reply_author'):item['reply_author'] = row['reply_author']
        if row.get('mentions'):item['mentions'] = row['mentions']
        out.append(item)
    return json.dumps(out, ensure_ascii=False, separators=(',', ':'))


def synthetic_rows(count=80):
    people = [('2' + str(10 ** 9 + 7919 * i), '群友%d号' % i) for i in range(12)]
    rows = []
    for i in range(count):
        uid, name = people[i % len(people)]
        row = {'message_id': str(8165862396903856000 + i), 'user_id': uid, 'nickname': name,
               'text': '这是第%d条比较普通的群聊消息内容' % i, 'received_at': 1700000000.0 + i}
        if i % 5 == 0:
            row['reply_to'] = str(8165862396903856000 + max(i - 3, 0))
        if i % 7 == 0:
            row['direct_targets'] = ['9000000000000000001' if i % 14 == 0 else people[(i + 1) % 12][0]]
        if i % 11 == 0:
            row.update(user_id='SELF', nickname='哈基米')
        rows.append(row)
    return rows


class JudgeWireTests(unittest.TestCase):
    def test_round_trip_ids_and_fields_survive_and_payload_shrinks(self):
        rows = synthetic_rows()
        seen = []
        def runner(task, base):
            seen.append(task)
            return {'selected_message_ids': ['3', '9']}
        picked = judge.select_messages(rows, '上一条', '.', runner, limit=None)
        # Short aliases '3'/'9' map back to the real platform ids.
        data = json.loads(seen[0])
        expected = judge.text_batch(rows)
        decoded = judge.decode_messages(data)
        self.assertEqual(len(decoded), len(expected))
        for got, want in zip(decoded, expected):
            self.assertEqual(got['text'], want['text'])
            self.assertEqual((got.get('user_id', ''), got.get('name', '')), (want['user_id'], want['name']))
            self.assertEqual(got.get('audience', ''), want['audience'])
            self.assertEqual(got.get('reply_author', ''), want['reply_author'])
            self.assertEqual(got.get('mentions', []), want['mentions'])
        self.assertEqual([r['message_id'] for r in decoded][:3], ['1', '2', '3'])
        self.assertNotIn('8165862396903856000', seen[0])
        self.assertEqual(sorted(picked), sorted([expected[2]['message_id'], expected[8]['message_id']]))
        new_bytes = len(json.dumps(data['messages'], ensure_ascii=False, separators=(',', ':')).encode()) \
            + len(json.dumps(data['speakers'], ensure_ascii=False, separators=(',', ':')).encode())
        old_bytes = len(legacy_judge_wire(expected).encode())
        self.assertLess(new_bytes, old_bytes * 0.8)

    def test_history_rows_share_one_speaker_table_and_back_references_resolve(self):
        history = [{'message_id': '100', 'user_id': '42', 'nickname': '群友8eed', 'text': '怎么也得凑个六翼'},
                   {'message_id': '102', 'user_id': 'SELF', 'nickname': '哈基米', 'text': '六翼炽天使那是高定配置了'}]
        rows = [{'message_id': '103', 'user_id': '42', 'nickname': '群友8eed', 'text': '又是断臂又是无貌', 'reply_to': '102'}]
        seen = []
        judge.select_messages(rows, '', '.', lambda task, base: seen.append(json.loads(task)) or {'selected_message_ids': ['1']},
                              history=history)
        data = seen[0]
        self.assertEqual(sum(1 for v in data['speakers'].values() if v[0] == '42'), 1)
        self.assertEqual(data['speakers']['SELF'], ['SELF', '哈基米'])
        exchange = judge.decode_messages(data, 'previous_exchange')
        self.assertEqual([r['message_id'] for r in exchange], ['h1', 'h2'])
        self.assertEqual(judge.decode_messages(data)[0]['reply_to'], 'h2')
        self.assertEqual(exchange[0]['user_id'], '42')


class ReplyReviewWireTests(unittest.TestCase):
    def packet(self):
        replies = [{'receipt': 'r1:0', 'message_id': 'bot-1', 'speaker': 'SELF', 'at': 1010.5, 'text': '我在听呢', 'reply_to': ['m1']},
                   {'receipt': 'r1:1', 'message_id': 'bot-2', 'speaker': 'SELF', 'at': 1012.25, 'text': '慢慢说', 'reply_to': ['m1']}]
        context = [{'kind': 'chat', 'user_id': '999', 'speaker': 'SELF', 'text': r['text'], 'at': r['at'],
                    'message_id': r['message_id'], 'reply_to': r['reply_to']} for r in replies]
        message = {'reaction_replies': ['我在听呢'], 'reaction_notes': [], 'id': 7, 'author': '101', 'author_name': '小树',
                   'self_id': '999', 'session': 'group:1', 'mentions': ['999'], 'at': 1000.0, 'reply_author': '999',
                   'reply_author_name': '', 'text': '陪我聊聊', 'message_id': 'm1', 'context': context}
        return {'window_id': 'w', 'messages': [message], 'sent_replies': replies}

    def test_wire_drops_constants_epochs_and_duplicate_context(self):
        packet = self.packet()
        wire = review.classifier_packet(packet)
        text = json.dumps(wire, ensure_ascii=False, separators=(',', ':'))
        self.assertNotIn('self_id', text)
        self.assertNotIn('999', text.replace('SELF', ''))
        self.assertNotIn('1010.5', text)
        message = wire['messages'][0]
        self.assertEqual((message['author'], message['mentions'], message['reply_author']), ('101', ['SELF'], 'SELF'))
        self.assertEqual(message['context'], ['b1', 'b2'])
        self.assertEqual([r['id'] for r in wire['sent_replies']], ['b1', 'b2'])
        self.assertEqual([r['at'] for r in wire['sent_replies']], [10, 12])
        self.assertEqual(message['at'], 0)
        self.assertEqual(wire['session'], 'group:1')
        self.assertNotIn('speaker', text)
        old = json.dumps(self.legacy(packet), ensure_ascii=False, separators=(',', ':'))
        self.assertLess(len(text), len(old) * 0.75)

    def test_context_missing_from_sent_replies_keeps_its_text(self):
        packet = self.packet()
        packet['sent_replies'].pop(0)
        wire = review.classifier_packet(packet)
        context = wire['messages'][0]['context']
        self.assertEqual(context[0]['text'], '我在听呢')
        self.assertEqual(context[0]['id'], 'b2')
        self.assertEqual(context[1], 'b1')

    @staticmethod
    def legacy(packet):
        messages = packet['messages']
        human = {str(r['message_id']): 'e' + str(i + 1) for i, r in enumerate(messages)}
        bot = {}
        for row in [*packet['sent_replies'], *[c for r in messages for c in r['context']]]:
            mid = str(row.get('message_id') or '')
            if mid and mid not in bot:bot[mid] = 'b' + str(len(bot) + 1)
        ref = lambda v: human.get(str(v), bot.get(str(v), 'outside_window'))
        def reply(row):
            item = {k: v for k, v in row.items() if k not in {'receipt', 'message_id'}}
            mid = str(row.get('message_id') or '')
            if mid in bot:item['id'] = bot[mid]
            if isinstance(item.get('reply_to'), list):item['reply_to'] = [ref(v) for v in item['reply_to']]
            return item
        out = {'messages': [], 'sent_replies': [reply(r) for r in packet['sent_replies']]}
        for i, row in enumerate(messages):
            item = {k: v for k, v in row.items() if k not in {'message_id', 'context', 'reaction_replies'}
                    and not (k in {'mentions', 'reply_to', 'reply_author', 'reaction_notes'} and not v)}
            item['id'] = 'e' + str(i + 1)
            item['context'] = [reply(v) for v in row['context']]
            out['messages'].append(item)
        return out


class NoteReviewPacketTests(unittest.TestCase):
    def test_difference_split_and_dedup_helpers(self):
        previous, current = '喜欢音乐。' * 5 + '曾在北京出差。', '喜欢音乐。' * 5 + '现居上海，养了一只橘色的猫。'
        deleted, written = impression_events._split(previous, current)
        self.assertEqual(impression_events._difference(previous, current), ('删去：' + deleted + '\n写入：' + written).strip())
        self.assertEqual(impression_events._without(current, written, '〔同写入〕'), current.replace(written, '〔同写入〕', 1))
        self.assertIn('〔同写入〕', impression_events._without(current, written, '〔同写入〕'))
        self.assertEqual(impression_events._without('x' * 30, '', '〔同写入〕'), 'x' * 30)
        self.assertRegex(impression_events._clock(1700000000), r'^\d\d-\d\d \d\d:\d\d$')

    def test_packet_has_no_epoch_or_self_id_and_shows_note_text_once(self):
        from hakimi.memory import daily_events
        from hakimi.memory.memory_store import MemoryStore
        from hakimi.social.social_schema import ensure_schema
        with tempfile.TemporaryDirectory() as tmp:
            index = MemoryStore(tmp)
            with index.db() as db:
                ensure_schema(db);db.executescript(impression_events.SCHEMA);daily_events.initialize(db)
            index.observe({'session_key': 'group:1', 'user_id': '101', 'text': '你好呀猫猫', 'message_id': 'src',
                           'received_at': 1000, 'direct_targets': ['999'], 'self_id': '999'})
            body = '这位群友偏爱音乐与旅行，说话直接，常常主动找我聊天，并愿意认真听我的回答。' * 3
            index.remember_manual({'kind': 'person', 'user_id': '101', 'key': 'facts', 'note': body},
                                  session='group:1', provider='gemini', now=1100, origin_message_id='src')
            with index.db() as db:
                row = db.execute("SELECT * FROM memory_originals WHERE kind='person'").fetchone()
                job = {'session': 'group:1', 'target': '101', 'record_id': row['record_id'], 'original_id': row['id'], 'noted_at': 1100}
                packet = impression_events._packet(db, job)
            self.assertIsNotNone(packet)
            self.assertEqual(packet['current_note'], '〔同写入〕')
            self.assertIn(body, packet['note_change'])
            event = packet['events'][0]
            self.assertNotIn('self_id', event)
            self.assertIsInstance(event['at'], str)
            self.assertEqual(event['mentions'], ['SELF'])
            self.assertTrue(event['by_target'])
            self.assertNotIn('reply_to', event)
            self.assertEqual(event['reaction_notes'], ['〔见current_note〕'])
            self.assertIsInstance(packet['written_at'], str)


if __name__ == '__main__':unittest.main()
