import copy
import json
import unittest
from hakimi.prompt.prompt_compaction import lossless_context, compact_tool_result


class ContextWireTests(unittest.TestCase):
    def test_identity_table_round_trip(self):
        messages = [dict(message_id=str(i), user_id='8112866014149232024',
                         nickname='群友9d04' if i < 8 else '群友9d04·新名字', time='10-30 18:15:00',
                         text='名字、消息原文及标点保持不变。', extra=None) for i in range(12)]
        del messages[2]['extra']
        messages.append(dict(messages[0]))
        original = copy.deepcopy(messages)
        table = lossless_context({'recent_context_messages': messages})['recent_context_messages']
        self.assertIn('speakers', table)
        restored = []
        for i, values in enumerate(table['rows']):
            row = dict(zip(table['columns'], values))
            identity = table['speakers'][row.pop('speaker')]
            row.update(zip(table['speaker_columns'], identity))
            for absent in table.get('missing_fields', {}).get(str(i), []):
                row.pop(table['columns'][absent])
            restored.append(row)
        restored = [restored[i] for i in table.get('message_order', range(len(restored)))]
        self.assertEqual(restored, original)
        self.assertEqual(messages, original)
        self.assertLess(len(json.dumps(table, ensure_ascii=False)), len(json.dumps(messages, ensure_ascii=False)))

    def test_json_tool_receipt_keeps_content_and_errors(self):
        data = {'name': '群友9d04', 'note': '原文\n包含"引号"', 'source': 'https://example.com/a', 'items': [1, None]}
        output = {'ok': False, 'stdout': json.dumps(data, ensure_ascii=False, indent=2), 'stderr': '原始错误'}
        before = copy.deepcopy(output)
        wire = compact_tool_result(output)
        self.assertEqual(wire['stdout'], data)
        self.assertEqual(wire['stderr'], output['stderr'])
        self.assertFalse(wire['ok'])
        self.assertEqual(output, before)
        for text in ['普通文字\n代码', '{"a":1,"a":2}', '{"x":NaN}', '123']:
            receipt = {'stdout': text}
            self.assertEqual(compact_tool_result(receipt), receipt)
