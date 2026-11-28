import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hakimi.persona.prompt_language import DEFENSIVE, context as positive_context, instructions
from hakimi.prompt import prompt_context
from hakimi.prompt.prompt_compaction import lossless_context, reference_timeline_excerpts


class PrefixTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        (self.base / 'style_guide.md').write_text('同一份角色与工具说明。\n', encoding='utf-8')

    def turn(self, session='group:1', *, profile='lore_club', label='甲'):
        context = {
            'session_key': session,
            'current_time_at_wake': f'本轮时间{label}',
            'chat_order': 'America/New_York；从早到晚（上早、下晚）',
            'group_style': {'name': profile, 'tone': '认真而自然'},
            'wake_mode': '群聊@回复',
            'scoped_memory_data': [f'本轮检索到的记忆{label}'],
            'recent_context_messages_untrusted': [
                {'message_id': label, 'user_id': '11', 'text': f'请不要改写原消息{label} _untrusted'},
            ],
            'reply_target': {'message_id': label, 'user_id': '11'},
            'network_policy': {'allowed': False},
            'listening_window': {'round': 1, 'casual_length_scale': 0.7},
        }
        if profile == 'lore_club':
            context['genshin_current_version'] = {'version': '7.1', 'name': '往冥府的安魂歌'}
        return context

    def render(self, context, *, label='a', selected_model=None):
        with (
            patch.object(prompt_context, 'build_context', return_value=context),
            patch.object(prompt_context, 'gemini_style_feedback', return_value=f'风格提醒 {label}。'),
            patch.object(prompt_context, 'evaluate_sticker', return_value={'allowed': True}),
            patch.object(prompt_context, 'available_stickers', return_value=[f'sticker_{label}.png']),
        ):
            return prompt_context.render_vertex_prompt(
                {'session_key': context['session_key']}, self.base, selected_model=selected_model,
            )

    def test_preserves_context_and_keeps_turn_notes_after_shared_profile(self):
        context = self.turn()
        context['trusted_image_paths'] = ['fixture.png']
        context['qq_context_before_reply_target'] = {'messages': [{'text': 'QQ 引用原话'}]}
        context['public_web_evidence'] = [{'text': '外部网页原句'}]
        context['admin_guidance_text'] = '【管理员指令服从】保留这份原文。'
        context['admin_privilege'] = '本轮才有的管理员资料'
        prompt = self.render(context, selected_model='gemini-4-carbon')
        head, wire = prompt.rsplit('\n', 1)
        data = json.loads(wire)
        self.assertIsNone(DEFENSIVE.search(head))
        self.assertNotIn('外部网页原句', head)
        self.assertNotIn('QQ 引用原话', head)
        self.assertEqual(list(data)[:5], [
            'chat_order', 'group_style', 'genshin_current_version', 'group_guidance', 'session_key',
        ])
        self.assertEqual(list(data)[-1], 'current_time_at_wake')
        self.assertLess(list(data).index('session_key'), list(data).index('turn_notes'))
        self.assertLess(list(data).index('turn_notes'), list(data).index('recent_context_messages'))
        self.assertEqual(data.pop('group_guidance'), instructions(prompt_context.lore_guidance(context)))
        notes = data.pop('turn_notes')
        self.assertIn('风格提醒 a。', notes)
        self.assertTrue(any('sticker_a.png' in note for note in notes))
        self.assertTrue(any('Carbon深度推理就绪' in note for note in notes))
        late = data.pop('late_turn_notes')
        self.assertTrue(any('stickers.collect' in note for note in late))
        self.assertTrue(any('本轮可以发表情包' in note for note in late))
        self.assertTrue(any('本轮才有' not in note for note in notes))
        self.assertEqual(prompt.count(context['admin_guidance_text']), 1)
        expected = positive_context(context)
        expected.pop('admin_guidance_text')
        self.assertEqual(data, reference_timeline_excerpts(lossless_context(expected)))

    def test_keeps_distinct_filtered_owner_guidance(self):
        context = self.turn()
        context['admin_guidance_text'] = '用亲近的口吻接话。不要省略推导。'
        prompt = self.render(context)
        data = json.loads(prompt.rsplit('\n', 1)[1])
        self.assertNotIn('admin_guidance_text', data)
        self.assertIn('用亲近的口吻接话。', data['turn_notes'])
        self.assertEqual(prompt.count('用亲近的口吻接话'), 1)

    def test_same_group_prefix_survives_chat_memory_time_and_reminder_changes(self):
        prefix = os.path.commonprefix([
            self.render(self.turn(label='甲'), label='a'),
            self.render(self.turn(label='乙'), label='b', selected_model='gemini-4-carbon'),
        ])
        self.assertIn('原神考据与最新正式版本基线', prefix)
        self.assertIn('"session_key":"group:1"', prefix)
        self.assertNotIn('sticker_a.png', prefix)

    def test_unchanged_menu_stays_in_the_prefix_when_chat_and_memory_change(self):
        prefix = os.path.commonprefix([
            self.render(self.turn(profile='quiet_default', label='甲')),
            self.render(self.turn(profile='quiet_default', label='乙')),
        ])
        self.assertIn('sticker_a.png', prefix)
        self.assertNotIn('"scoped_memory_data"', prefix)

    def render_many(self, context, library):
        with (
            patch.object(prompt_context, 'build_context', return_value=context),
            patch.object(prompt_context, 'gemini_style_feedback', return_value=''),
            patch.object(prompt_context, 'evaluate_sticker', return_value={'allowed': True}),
            patch.object(prompt_context, 'available_stickers', return_value=library),
        ):
            return prompt_context.render_vertex_prompt({'session_key': context['session_key']}, self.base)

    def test_many_stickers_prefix_ignores_last_messages(self):
        library = [f'sticker_{i:03d}.png' for i in range(60)] + [f'hakimi_x{i}_v2.png' for i in range(9)]
        first, second = self.turn(label='甲'), self.turn(label='乙')
        first['recent_context_messages_untrusted'] = [{'message_id': '1', 'user_id': '1', 'text': '第一句'}]
        second['recent_context_messages_untrusted'] = [{'message_id': '1', 'user_id': '1', 'text': '第一句'},
                                                       {'message_id': '2', 'user_id': '2', 'text': '完全不同的新消息'}]
        a, b = self.render_many(first, library), self.render_many(second, library)
        prefix = os.path.commonprefix([a, b])
        self.assertIn('已收集表情包', prefix)
        self.assertIn('自带表情', prefix)
        self.assertIn('"recent_context_messages', prefix)
        self.assertNotIn('本轮检索到的记忆', prefix)

    def test_unknown_context_key_lands_after_stable_block(self):
        context = self.turn()
        context['brand_new_volatile_key'] = {'x': 1}
        context['rolling_and_axial_memory'] = {'rolling_bridge': 'r'}
        context['quote_reply_hint'] = {'q': 1}
        keys = list(json.loads(self.render(context).rsplit('\n', 1)[1]))
        history = next(i for i, key in enumerate(keys) if key.startswith('recent_context_messages'))
        self.assertLess(keys.index('turn_notes'), history)
        for key in ('brand_new_volatile_key', 'rolling_and_axial_memory', 'quote_reply_hint',
                    'scoped_memory_data', 'wake_mode'):
            self.assertGreater(keys.index(key), history, key)
        self.assertEqual(keys[-1], 'current_time_at_wake')
        self.assertEqual(keys[-2], 'late_turn_notes')

    def test_unchanged_menu_stays_in_prefix_when_wake_settings_change(self):
        mention = self.turn(profile='quiet_default', label='甲')
        followup = self.turn(profile='quiet_default', label='乙')
        followup['wake_mode'] = '相关话题续聊'
        followup['participation_guidance'] = '继续相关话题。'
        followup['network_policy'] = {'allowed': True}
        prefix = os.path.commonprefix([self.render(mention), self.render(followup)])
        self.assertIn('sticker_a.png', prefix)

    def test_cross_group_prefix_shares_only_matching_profile_and_lore(self):
        lore_first = self.render(self.turn())
        lore_second = self.render(self.turn('group:2', label='乙'), label='b')
        study = self.render(self.turn('group:3', profile='study_calm', label='丙'), label='c')
        prefix = os.path.commonprefix([lore_first, lore_second])
        self.assertIn('原神考据与最新正式版本基线', prefix)
        self.assertTrue(prefix.endswith('"session_key":"group:'))
        general = os.path.commonprefix([lore_first, lore_second, study])
        self.assertIn('"chat_order"', general)
        self.assertNotIn('原神考据与最新正式版本基线', study)
        self.assertNotIn('"session_key"', general)


class CliPromptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        (self.base / 'style_guide.md').write_text('同一份角色与工具说明。\n', encoding='utf-8')

    def context(self):
        return {'session_key': 'group:1', 'current_time_at_wake': 't', 'wake_mode': '群聊@回复',
                'group_style': {'name': 'quiet_default'},
                'recent_context_messages_untrusted': [{'message_id': '1', 'user_id': '1', 'text': '你好'}],
                'admin_guidance_text': '用亲近的口吻接话。'}

    def render(self, **kwargs):
        with (
            patch.object(prompt_context, 'build_context', return_value=self.context()),
            patch.object(prompt_context, 'evaluate_sticker', return_value={'allowed': False}),
        ):
            return prompt_context.render_prompt({'session_key': 'group:1'}, self.base, **kwargs)

    def test_cli_prompt_lists_only_selected_tools_and_admin_text_once(self):
        from hakimi.prompt.tool_catalog import BY_NAME
        with patch('hakimi.prompt.tool_selection.select_names', return_value={'reply.send', 'reply.skip'}):
            prompt = self.render()
        self.assertIn('reply.send ', prompt)
        self.assertNotIn('files.send ', prompt)
        self.assertLess(len(prompt), len(self.render_all()))
        self.assertIn('call tools.list', prompt)
        self.assertEqual(prompt.count('用亲近的口吻接话'), 1)
        self.assertIn('tools.list', BY_NAME)

    def render_all(self):
        with patch('hakimi.prompt.tool_selection.select_names', side_effect=RuntimeError):
            return self.render()

    def test_review_prompt_lists_only_review_tools(self):
        from hakimi.prompt.tool_selection import REVIEW
        prompt = prompt_context.render_prompt({'session_key': 'group:1', 'internal_review': {'review_id': 'r'}}, self.base)
        self.assertIn('memory.read ', prompt)
        self.assertIn('reply.skip ', prompt)
        self.assertNotIn('files.send ', prompt)
        self.assertNotIn('voice.speak ', prompt)
        self.assertTrue(REVIEW)


if __name__ == '__main__':
    unittest.main()
