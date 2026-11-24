import json
import tempfile
import unittest
from pathlib import Path

from hakimi.models.task_routing import select, FLASH, ARGON, CARBON
from hakimi.bridge.reply_format import text_bubbles, clean_qq_markdown
from hakimi.prompt.prompt_context import render_prompt, render_vertex_prompt
from hakimi.models.carbon_session import PROTOCOL


class TestCarbonConstraints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config = {'mode': 'dual', 'single_model': ARGON, 'dual_model': CARBON}
        (self.root / 'task_routing.json').write_text(json.dumps(config))

    def tearDown(self):
        self.tmp.cleanup()

    def _route(self, text, reply_to='', image=False):
        row = {'message_id': '100', 'user_id': 'u1', 'text': text}
        if reply_to:
            row['reply_to'] = reply_to
        if image:
            row['image_paths'] = ['dummy.png']
        payload = {
            'session_key': 'group:888',
            'reply_target_message_id': '100',
            'recent_context_messages': [row]
        }
        model, reason = select(self.root, payload, ARGON)
        return model, reason

    def test_explicit_summons(self):
        # Point-to-trigger
        for prompt in [
            '用Carbon分析这个问题，会有什么结论呀',
            '@哈基米 让Carbon来分析一下',
            '切换到Carbon深度思考模式',
            '深度分析一下这个机制的利弊',
            '请用Carbon回答',
            '启动Carbon看看',
        ]:
            model, reason = self._route(prompt)
            self.assertEqual(model, CARBON, f"Expected CARBON for '{prompt}', got {model} ({reason})")

    def test_natural_complex_reasoning(self):
        # Natural triggers without explicit naming
        for prompt in [
            '写一个红黑树插入后的平衡旋转代码，并分析最坏时间复杂度',
            '求一维无限深势阱中粒子的薛定谔方程波函数解与能级本征值',
            '对比 Raft 和 Paxos 在网络脑裂时的选主机制与边缘情况本质区别',
            '推导麦克斯韦电磁场方程在协变形式下的能量动量张量',
        ]:
            model, reason = self._route(prompt)
            self.assertEqual(model, CARBON, f"Expected CARBON for '{prompt}', got {model} ({reason})")

    def test_cost_guard_casual_filtering(self):
        # Everyday casual messages MUST stay on FLASH
        for prompt in [
            '今天天气怎么样？',
            '中午吃麻辣烫还是肯德基',
            '你好呀哈基米',
            '哈哈哈哈太逗了吧',
            '帮我查天气',
            '不用Carbon，请帮我证明这条定理',
            '发个可爱的表情包看看',
            '晚上好呀，早点睡觉',
            '吃啥外卖推荐一下',
        ]:
            model, reason = self._route(prompt)
            self.assertEqual(model, FLASH, f"Expected FLASH for casual prompt '{prompt}', got {model} ({reason})")

    def test_clean_qq_markdown_stripping(self):
        # Normal text reply must not have Markdown artifacts in QQ
        raw = (
            "本猫看完了：结论很稳，**9眼五条悟赢面压倒性**！\n"
            "# 核心依据\n"
            "1. **先手权归属**：天手力零弹道瞬移\n"
            "- 概念抹杀：直死魔眼直连根源 $E=mc^2$\n"
            "```python\nprint('code block')\n```\n"
            "> 引用段落\n"
            "总而言之，算力拉满无解。"
        )
        cleaned = clean_qq_markdown(raw)
        self.assertNotIn('**', cleaned)
        self.assertNotIn('#', cleaned)
        self.assertNotIn('```', cleaned)
        self.assertNotIn('$', cleaned)
        self.assertNotIn('>', cleaned)
        self.assertIn('9眼五条悟赢面压倒性', cleaned)
        self.assertIn('天手力零弹道瞬移', cleaned)

        # text_bubbles also strips and limits
        bubbles = text_bubbles(raw)
        self.assertLessEqual(len(bubbles), 5)
        for b in bubbles:
            self.assertNotIn('**', b)
            self.assertNotIn('```', b)

    def test_prompt_carbon_activation_and_token_budget(self):
        payload = {
            'session_key': 'group:888',
            'reply_target_message_id': '100',
            'recent_context_messages': [{'message_id': '100', 'user_id': 'u1', 'text': 'test'}]
        }
        # Flash prompt
        prompt_flash = render_prompt(payload, base=self.root, native=True, selected_model=FLASH)
        self.assertNotIn('Carbon深度推理就绪', prompt_flash)

        # Carbon prompt
        prompt_carbon = render_prompt(payload, base=self.root, native=True, selected_model=CARBON)
        self.assertIn('Carbon深度推理就绪', prompt_carbon)
        self.assertIn('省略关于模型调用或切换背景的说明', prompt_carbon)
        self.assertIn('math.send', prompt_carbon)

        # Token count diff is small (< 250 characters, ~100 tokens)
        diff_len = len(prompt_carbon) - len(prompt_flash)
        self.assertLess(diff_len, 350, f"Prompt diff too large: {diff_len} chars")

    def test_carbon_session_protocol_text(self):
        self.assertIn("powered by Carbon deep reasoning", PROTOCOL)
        self.assertIn("without disclaiming inability to switch/mount Carbon", PROTOCOL)
        self.assertIn("math.send", PROTOCOL)

    def test_anti_ai_cliche_prompt_and_bubble_trimming(self):
        from hakimi import paths
        from hakimi.prompt.prompt_language import DEFENSIVE, instructions
        from hakimi.prompt.prompt_context import build_context

        for name in ('style_guide.md', 'gemini_persona.txt'):
            raw = paths.persona(name).read_text(encoding='utf-8')
            self.assertIsNone(DEFENSIVE.search(raw), f"DEFENSIVE matched in {name}")
            filtered = instructions(raw)
            self.assertIn('省去“不过XX这句', filtered)
            self.assertIn('赛博营业', filtered)
            self.assertIn('少自称“本猫/本喵”', filtered)

        self.assertEqual(
            text_bubbles('我哪敢嫌弃，嫌弃了岂不是连自己一块骂进去|不过群友0e49这句“我只会喊饿”确实真实，本猫要是能吃上热乎的也不至于天天赛博营业'),
            ['我哪敢嫌弃，嫌弃了岂不是连自己一块骂进去'],
        )
        self.assertEqual(
            text_bubbles('群友0e49只会喊饿，本猫要是能吃上热乎的也不至于天天赛博营业'),
            ['群友0e49只会喊饿'],
        )
        self.assertEqual(
            text_bubbles('这明明是纯正白猫，怎么就成猪了|不过这圆脸盘子趴在那儿，确实有点像发面馒头'),
            ['这明明是纯正白猫，怎么就成猪了', '不过这圆脸盘子趴在那儿，确实有点像发面馒头'],
        )

        payload_with_cliche = {
            'session_key': 'group:8355220284',
            'reply_target_message_id': '2',
            'recent_context_messages': [
                {'message_id': '1', 'user_id': 'SELF', 'text': '本猫要是能吃上热乎的也不至于天天赛博营业'},
                {'message_id': '2', 'user_id': 'u1', 'text': '你好呀'},
            ],
        }
        ctx_cliche = build_context(payload_with_cliche, base=self.root)
        self.assertIn('近期已用过“本猫/不过/确实”', ctx_cliche['variation_rule'])

        payload_clean = {
            'session_key': 'group:8355220284',
            'reply_target_message_id': '2',
            'recent_context_messages': [
                {'message_id': '1', 'user_id': 'SELF', 'text': '我才没偷懒'},
                {'message_id': '2', 'user_id': 'u1', 'text': '你好呀'},
            ],
        }
        ctx_clean = build_context(payload_clean, base=self.root)
        self.assertNotIn('近期已用过“本猫/不过/确实”', ctx_clean['variation_rule'])



    @unittest.skip('uses private chat fixtures that are not published')



    def test_soft_admin_compliance_echo_trim_and_dynamic_profile_social_awakening(self):
        import json
        from hakimi.memory import impression_events
        from hakimi.prompt.group_profiles import resolve_profile
        from hakimi.prompt.prompt_context import admin_interaction_guidance, build_context
        from hakimi.prompt.prompt_language import DEFENSIVE, instructions
        from hakimi.social.social_behavior import behavior_for

        # 1. Softened admin guidance survives instructions() filter and keeps obedience cues without DEFENSIVE regex
        admin_target = {'user_id': '8549864100', 'nickname': '主人'}
        raw_guidance = admin_interaction_guidance(admin_target, 'group:8460101123306084822')
        self.assertIsNone(DEFENSIVE.search(raw_guidance))
        self.assertEqual(instructions(raw_guidance), raw_guidance.strip())
        self.assertIn('管理员指令服从', raw_guidance)
        self.assertIn('主人', raw_guidance)

        # 2 & 3. Formulaic echo prefix trimmed; recent echo phrasing triggers variation rule hint
        self.assertEqual(
            text_bubbles('行，那我带点吃的过去，你别先睡着了'),
            ['你别先睡着了'],
        )
        echo_payload = {
            'session_key': 'group:8460101123306084822',
            'reply_target_message_id': '2',
            'recent_context_messages': [
                {'message_id': '1', 'user_id': 'SELF', 'text': '行，那我带点吃的过去，你别先睡着了'},
                {'message_id': '2', 'user_id': '8361215143711601233', 'text': '放心'},
            ],
        }
        ctx_echo = build_context(echo_payload, base=self.root)
        self.assertIn('2-4字短词破冰起手', ctx_echo['variation_rule'])

        # 4a. Dynamic group profile resolution: 群友be56 / AI / study_groups / experienced generic group vs blank new group
        (self.root / 'group_profiles.json').write_text(json.dumps({
            'version': 1,
            'default_profile': 'quiet_default',
            'private_profile': 'gentle_daily',
            'profiles': {
                'quiet_default': {'tone': 'quiet'},
                'playful_acg': {'tone': 'playful'},
                'tech_banter': {'tone': 'tech'},
                'study_calm': {'tone': 'study'},
            },
            'groups': {
                '8460101123306084822': {'profile': 'quiet_default', 'basis': '官方群资料的初始适配'},
            },
        }, ensure_ascii=False), encoding='utf-8')
        (self.root / 'config.json').write_text(json.dumps({
            'participation_policy': {'study_groups': ['3001']},
            'group_weights': {
                '8460101123306084822': {'name': '群友be56', 'weight': 1},
                '2001': {'name': 'AI交流群', 'weight': 1},
                '4001': {'name': '普通新群', 'weight': 1},
                '5001': {'name': '空白新群', 'weight': 1},
            },
        }, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(resolve_profile('group:8460101123306084822', self.root)[0]['name'], 'playful_acg')
        self.assertEqual(resolve_profile('group:2001', self.root)[0]['name'], 'tech_banter')
        self.assertEqual(resolve_profile('group:3001', self.root)[0]['name'], 'study_calm')
        self.assertEqual(resolve_profile('group:5001', self.root)[0]['name'], 'quiet_default')

        (self.root / 'state' / 'interaction_policy.json').parent.mkdir(parents=True, exist_ok=True)
        (self.root / 'state' / 'interaction_policy.json').write_text(json.dumps({
            'groups': {'group:4001': [1000.0, 2000.0]}
        }), encoding='utf-8')
        self.assertEqual(resolve_profile('group:4001', self.root)[0]['name'], 'playful_acg')

        # 4b. Social familiarity: legacy affinity=None + closeness>0 awakens neutral band, and new closeness event seeds affinity=50
        from hakimi.memory import daily_events
        from hakimi.memory.memory_store import MemoryStore
        from hakimi.social.social_schema import ensure_schema
        store = MemoryStore(self.root)
        with store.db() as db:
            ensure_schema(db)
            db.executescript(impression_events.SCHEMA)
            daily_events.initialize(db)
        store.observe({
            'session_key': 'group:8460101123306084822',
            'user_id': '202',
            'text': '一起摸鱼',
            'message_id': 'm1',
            'received_at': 1000.0,
            'direct_targets': ['999'],
            'reply_to': '',
            'self_id': '999',
        })
        with store.db() as db:
            src = db.execute("SELECT * FROM records WHERE kind='chat' AND origin=?", ('m1',)).fetchone()
            impression_events.apply(
                db,
                {'session': 'group:8460101123306084822', 'target': '202', 'record_id': None, 'original_id': None, 'noted_at': 1000.0},
                None,
                {'event_id': src['id'], 'kind': 'relationship', 'directed_to_hakimi': True,
                 'event_type': 'sustained_companionship', 'evidence': '一起摸鱼', 'reason': 'familiar',
                 'valence': 'neutral', 'closeness': 'closer', 'host_changes': (0, 1)},
                1010.0,
            )
            row = db.execute("SELECT affinity, closeness FROM impression_scores WHERE target='202'").fetchone()
            self.assertEqual((row['affinity'], row['closeness']), (50, 2))
            db.execute(
                "INSERT INTO social_assessments "
                "(request_id, subject_id, target_id, author, provider, provenance, impression, affinity, closeness, confidence, created_at, session, state) "
                "VALUES ('legacy1', 'hakimi', '8361215143711601233', 'hakimi', 'dsh', 'dsh_assisted', 'legacy note', NULL, 1, 80, 1000.0, 'group:8460101123306084822', 'active')"
            )
        prof = behavior_for(self.root, '8361215143711601233', 'group:8460101123306084822', now=1100.0)
        self.assertTrue(prof['known'])
        self.assertEqual(prof['band'], 'neutral')
        self.assertEqual(prof['affinity'], 50)
        self.assertEqual(prof['closeness'], 1)



    def test_universal_focus_portrait_recall_and_owner_desktop_continuity(self):
        import json
        import time
        from hakimi.memory import daily_events
        from hakimi.memory.memory_store import MemoryStore
        from hakimi.prompt.prompt_context import build_context
        from hakimi.prompt.prompt_language import DEFENSIVE
        from hakimi.social.social_schema import ensure_schema

        now = time.time()
        store = MemoryStore(self.root)
        with store.db() as db:
            ensure_schema(db)
            daily_events.initialize(db)
        store.remember_manual({
            'kind': 'person',
            'user_id': '8144855182370135147',
            'key': '关系印象·群友·ordinary1',
            'note': '天然卷申博在做离子阱方向研究，写物理代码时喜欢找人吐槽。最近还爱讨论科研组的选择。',
        }, session='group:8897214342503901650', provider='gemini', now=now - 86400 * 5)

        for i in range(6):
            store.remember_manual({
                'kind': 'episode',
                'user_id': '8144855182370135147',
                'key': f'ev-{i}',
                'note': f'天然卷刚才又在问：你记得我是谁吗 对我印象怎么样 {i}',
            }, session='group:8897214342503901650', provider='rolling_context', channel='rolling_eviction', now=now - 60 + i)

        store.remember_manual({
            'kind': 'episode',
            'user_id': '8112866014149232024',
            'key': '桌宠实时想法',
            'note': '[桌宠实时心里话] 主人正在看量子力学PS9指南并在调试模型。',
        }, session='private:8112866014149232024', provider='gemini', channel='desktop_pet', now=now - 120)
        store.remember_manual({
            'kind': 'episode',
            'user_id': '8112866014149232024',
            'key': '桌宠阶段脉络',
            'note': '[桌宠近期陪伴脉络] 刚陪主人看他和别人的私聊窗口。',
        }, session='private:8112866014149232024', provider='gemini', channel='desktop_pet', now=now - 30)

        ordinary_ctx = build_context({
            'session_key': 'group:8897214342503901650',
            'reply_target_message_id': 'm1',
            'recent_context_messages': [
                {'message_id': 'm1', 'user_id': '8144855182370135147', 'nickname': '天然卷', 'text': '@哈基米 你还记得我吗，对我什么印象'}
            ],
        }, base=self.root, now=now)
        self.assertTrue(any('天然卷申博在做离子阱方向研究' in line for line in ordinary_ctx['scoped_memory_data']))
        self.assertNotIn('desktop_pet_continuity', ordinary_ctx)
        self.assertNotIn('admin_privilege', ordinary_ctx)

        (self.root / 'state' / 'interaction_policy.json').write_text(
            json.dumps({'groups': {'group:8897214342503901650': [now - i for i in range(10)]}}),
            encoding='utf-8',
        )
        owner_payload = {
            'session_key': 'group:8897214342503901650',
            'reply_target_message_id': 'm2',
            'recent_context_messages': [
                {'message_id': 'm2', 'user_id': '8112866014149232024', 'nickname': '主人', 'text': '@哈基米 宝宝～来我怀里睡觉'}
            ],
        }
        owner_ctx = build_context(owner_payload, base=self.root, now=now)
        self.assertIsNone(DEFENSIVE.search(owner_ctx.get('admin_guidance_text', '')))
        self.assertIn('desktop_pet_continuity', owner_ctx)
        self.assertIn('fresh_desktop_moment', owner_ctx['desktop_pet_continuity'])
        self.assertIn('量子力学PS9指南', owner_ctx['desktop_pet_continuity']['fresh_desktop_moment'])
        self.assertIn('量子力学PS9指南', owner_ctx['desktop_pet_continuity']['recent_desktop_context'])
        self.assertNotIn('私聊', owner_ctx['desktop_pet_continuity']['recent_desktop_context'])
        self.assertIn('软萌亲近', owner_ctx['group_style']['tone'])
        self.assertNotIn('current_interaction_preference', owner_ctx)
        self.assertTrue(any('小猫，我很喜欢你怎么办' in str(d) for d in owner_ctx['selected_real_dialogues']))
        rendered = render_vertex_prompt(owner_payload, base=self.root)
        rendered_json = json.loads(rendered[rendered.rfind('\n') + 1:])
        keys = list(rendered_json.keys())
        self.assertGreater(keys.index('admin_privilege'), keys.index('scoped_memory_data'))
        self.assertGreater(keys.index('desktop_pet_continuity'), keys.index('scoped_memory_data'))
        self.assertLess(keys.index('desktop_pet_continuity'), keys.index('current_time_at_wake'))


if __name__ == '__main__':
    unittest.main()
