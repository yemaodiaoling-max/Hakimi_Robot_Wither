import copy
import json
import unittest

from hakimi.memory.rolling_context import build_memory_axes, timeline_row
from hakimi.prompt.prompt_compaction import lossless_context, reference_timeline_excerpts

NOTES = [
    '[M12] 小明·记2025-01-02 03:04：喜欢喝无糖乌龙茶，约定每周五提醒他买票，称呼他阿明。' * 2,
    '群档案：这个群主要讨论原神考据和版本更新，偶尔聊音乐游戏，发言风格偏轻松，爱用表情。',
    '[M7] 小红·记2025-02-03 10:00：最近在准备考试，偏好安静的聊天氛围。',
    '旧版记忆：小刚人物设定，约定周末一起开黑。' * 3,
    '短',
]


def expand(wire, notes):
    """Rebuild the literal rolling_and_axial_memory from a referenced wire form."""
    axes = copy.deepcopy(wire['rolling_and_axial_memory'])
    by_mid = {n.split(']')[0][1:]: n for n in notes if n.startswith('[M')}
    for cat, items in axes['vertical_threads'].items():
        out = []
        for item in items:
            if isinstance(item, dict) and 'memory_id' in item:
                item = by_mid[item['memory_id']]
            elif isinstance(item, dict) and 'note' in item:
                item = notes[item['note']]
            out.append(item)
        axes['vertical_threads'][cat] = out
    timeline = axes['horizontal_timeline']
    if isinstance(timeline, dict):
        out = []
        for row in timeline['rows']:
            if isinstance(row, int):
                row = timeline_row(notes[row])
            elif isinstance(row, str):
                row = timeline_row(by_mid[row])
            out.append(row)
        timeline = out
    axes['horizontal_timeline'] = timeline
    axes.pop('note_refs', None)
    return axes


class MemoryAxesReferenceTests(unittest.TestCase):
    def context(self, notes=NOTES):
        axes = build_memory_axes(list(notes), {'rolling_summary': '摘要'}, [], {}, now=1.0)
        return {'scoped_memory_data': list(notes), 'rolling_and_axial_memory': axes}, axes

    def test_every_original_is_reconstructible(self):
        context, axes = self.context()
        wire = lossless_context(context)
        self.assertEqual(expand(wire, NOTES), axes)
        self.assertEqual(wire['scoped_memory_data'], NOTES)

    def test_idless_notes_are_referenced_by_position(self):
        context, _ = self.context()
        wire = lossless_context(context)
        text = json.dumps(wire['rolling_and_axial_memory'], ensure_ascii=False)
        for note in NOTES[1:2] + NOTES[3:4]:
            self.assertNotIn(note[:30], text)
        threads = wire['rolling_and_axial_memory']['vertical_threads']
        flat = threads['person_and_commitments'] + threads['topics_and_episodes']
        self.assertIn({'note': 1}, flat)
        self.assertIn({'note': 3}, flat)
        self.assertIn({'memory_id': 'M7'}, flat)
        self.assertIn('note_refs', wire['rolling_and_axial_memory'])

    def test_no_legend_when_no_positional_reference(self):
        notes = ['[M1] 甲·记2025-01-01 00:00：' + '内容' * 30, '[M2] 乙·记2025-01-02 00:00：' + '资料' * 30]
        context, axes = self.context(notes)
        wire = lossless_context(context)
        self.assertNotIn('note_refs', wire['rolling_and_axial_memory'])
        self.assertEqual(expand(wire, notes), axes)

    def test_idempotent(self):
        context, _ = self.context()
        once = lossless_context(context)
        self.assertEqual(lossless_context(once), once)
        self.assertEqual(reference_timeline_excerpts(once), once)

    def test_tiny_notes_stay_literal(self):
        notes = ['短', '小']
        context, axes = self.context(notes)
        wire = lossless_context(context)
        self.assertEqual(wire['rolling_and_axial_memory']['vertical_threads'], axes['vertical_threads'])
        self.assertNotIn('note_refs', wire['rolling_and_axial_memory'])

    def test_input_not_mutated(self):
        context, _ = self.context()
        before = copy.deepcopy(context)
        lossless_context(context)
        self.assertEqual(context, before)


if __name__ == '__main__':
    unittest.main()
