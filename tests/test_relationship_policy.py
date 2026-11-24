"""Small offline checks for host-owned relationship event scores."""
import unittest
from hakimi.memory import daily_events, post_reply_review
from hakimi.memory.relationship_policy import (
    CLASSIFICATION_PROTOCOL, EVENT_TYPES, normalize_event, score_change,
)
from hakimi.prompt.prompt_language import DEFENSIVE, prompt


class RelationshipPolicyTests(unittest.TestCase):
    def event(self, event_type, *, reply_texts=(), note_texts=(), **extra):
        return normalize_event(dict(event_type=event_type, evidence="actual source", **extra),
                               "actual source", reply_texts=reply_texts, note_texts=note_texts)

    def test_social_events_use_trusted_reaction_and_fixed_values(self):
        for kind in ("banter", "roleplay", "affection", "ordinary_chat", "personal_fact", "routine_question"):
            raw=dict(event_type=kind, evidence="actual source", reaction_type="positive",
                     reaction_source="reply", reaction_evidence="I welcome this", valence="positive",
                     intensity="strong", closeness="closer", host_changes=(99,99),
                     host_impact="major", host_reaction_verified=True)
            first=normalize_event(raw,"actual source")
            self.assertEqual(first["host_changes"],(0,0))
            self.assertFalse(first["host_reaction_verified"])
            restored=normalize_event(first,"actual source",reply_texts=("I welcome this",))
            expected=(1,1) if kind in {"banter","roleplay","affection"} else (0,1)
            self.assertEqual(restored["host_changes"],expected)
            self.assertEqual(tuple(score_change(f,50,restored) for f in ("affinity","closeness")),expected)
            for reaction in ("neutral","unclear"):
                self.assertEqual(self.event(kind,reply_texts=("I welcome this",),reaction_type=reaction,
                    reaction_source="reply",reaction_evidence="I welcome this")["host_changes"],(0,0))
            annoyed=self.event(kind,reply_texts=("I am annoyed",),reaction_type="annoyed",
                reaction_source="reply",reaction_evidence="I am annoyed")
            self.assertEqual(annoyed["host_changes"],(-1,0))
            rejected=self.event(kind,reply_texts=("Stop doing that",),reaction_type="rejected",
                reaction_source="reply",reaction_evidence="Stop doing that")
            self.assertEqual(rejected["host_changes"],(-1,-1) if kind in {"banter","roleplay","affection"} else (-1,0))
            engaged=self.event(kind,note_texts=("we have a good back and forth",),reaction_type="engaged",
                reaction_source="note",reaction_evidence="good back and forth")
            self.assertEqual(engaged["host_changes"],(0,1) if kind in {"ordinary_chat","personal_fact","routine_question"} else (0,0))
        self.assertEqual(self.event("praise",host_changes=(99,99))["host_changes"],(1,0))
        self.assertEqual(self.event("praise",reply_texts=("Stop flattering me",),reaction_type="annoyed",
            reaction_source="reply",reaction_evidence="Stop flattering me")["host_changes"],(0,0))
        self.assertEqual(self.event("unknown")["host_changes"],(0,0))

    def test_confiding_and_graded_negative_events(self):
        self.assertEqual(self.event("confiding_or_seeking_help")["host_changes"],(0,0))
        self.assertEqual(self.event("confiding_or_seeking_help",reply_texts=("抱抱你",),
            reaction_type="positive",reaction_source="reply",reaction_evidence="抱抱你")["host_changes"],(1,1))
        self.assertEqual(self.event("confiding_or_seeking_help",reply_texts=("慢慢说我在听",),
            reaction_type="engaged",reaction_source="reply",reaction_evidence="慢慢说我在听")["host_changes"],(0,1))
        self.assertEqual(self.event("teasing_or_bossing")["host_changes"],(-1,0))
        self.assertEqual(self.event("teasing_or_bossing",reply_texts=("我赢在灵活",),
            reaction_type="annoyed",reaction_source="reply",reaction_evidence="我赢在灵活")["host_changes"],(-1,0))
        self.assertEqual(self.event("teasing_or_bossing",reply_texts=("哈哈好吧",),
            reaction_type="positive",reaction_source="reply",reaction_evidence="哈哈好吧")["host_changes"],(1,1))
        self.assertEqual(self.event("teasing_or_bossing",reply_texts=("知道了",),
            reaction_type="neutral",reaction_source="reply",reaction_evidence="知道了")["host_changes"],(0,0))
        self.assertEqual(self.event("teasing_or_bossing",reply_texts=("别这样叫我",),
            reaction_type="rejected",reaction_source="reply",reaction_evidence="别这样叫我")["host_changes"],(-2,-1))
        self.assertEqual(self.event("dismissive_or_nitpick")["host_changes"],(-1,0))
        self.assertEqual(self.event("dismissive_or_nitpick",reply_texts=("行吧那我改改",),
            reaction_type="engaged",reaction_source="reply",reaction_evidence="行吧那我改改")["host_changes"],(0,0))
        self.assertEqual(self.event("pestering_or_spam")["host_changes"],(-2,-1))
        self.assertEqual(self.event("pestering_or_spam",reply_texts=("别刷了烦死了",),
            reaction_type="annoyed",reaction_source="reply",reaction_evidence="别刷了烦死了")["host_changes"],(-1,0))

    def test_exact_and_normalized_source_and_matching_reaction_channel_required(self):
        for raw in ({"event_type":"substantial_help","evidence":"invented"},
                    {"event_type":"substantial_help","evidence":""},
                    {"event_type":"invented","evidence":"actual source"}):
            event=normalize_event(raw,"actual source")
            self.assertEqual(event["host_changes"],(0,0))
            self.assertEqual(score_change("affinity",50,event),0)
        normalized=normalize_event(
            {"event_type":"teasing_or_bossing","evidence":"你个废物和奶龙几几开",
             "reaction_type":"annoyed","reaction_source":"reply","reaction_evidence":"我赢在灵活，它赢在圆。"},
            "[CQ:at,qq=999] @哈基米 你个废物和奶龙几几开",
            reply_texts=("这么说吧，我赢在灵活", "它赢在圆"),
        )
        self.assertTrue(normalized["host_evidence_verified"])
        self.assertTrue(normalized["host_reaction_verified"])
        self.assertEqual(normalized["host_changes"],(-1,0))
        for extra in ({"reaction_evidence":"invented"}, {"reaction_evidence":""}, {"reaction_source":"note"}):
            raw=dict(event_type="affection",evidence="actual source",reaction_type="positive",
                     reaction_source="reply",reaction_evidence="I welcome this")
            raw.update(extra)
            event=normalize_event(raw,"actual source",reply_texts=("I welcome this",))
            self.assertEqual(event["host_changes"],(0,0))
        self.assertEqual(len(EVENT_TYPES),22)

    def test_prompts_survive_defensive_filter_without_loss(self):
        for text in (CLASSIFICATION_PROTOCOL, post_reply_review.INSTRUCTION, daily_events.INSTRUCTION):
            self.assertIsNone(DEFENSIVE.search(text))
            self.assertEqual(prompt(text), text)

    def test_reversed_help_and_support_direction_are_reclassified_by_host(self):
        ask_img = normalize_event(
            {"event_type": "substantial_help", "evidence": "@哈基米 识图",
             "reaction_type": "unclear", "reason": "用户明确请求识图，SELF 回复了图片内容“草神纳西妲呀”，构成一次有完成结果的实质帮助。"},
            "[引用消息] @哈基米 识图",
        )
        self.assertEqual(ask_img["event_type"], "routine_question")
        self.assertEqual(ask_img["host_changes"], (0, 0))

        ask_gacha = normalize_event(
            {"event_type": "practical_help", "evidence": "@哈基米 米提亚可以抽吗",
             "reaction_type": "engaged", "reaction_source": "reply", "reaction_evidence": "0命提升不大",
             "reason": "用户直接向哈基米询问抽卡建议，哈基米给出多轮具体分析。"},
            "@哈基米 米提亚可以抽吗",
            reply_texts=("0命提升不大，真爱党可抽",),
        )
        self.assertEqual(ask_gacha["event_type"], "routine_question")
        self.assertEqual(ask_gacha["host_changes"], (0, 1))

        ask_meme = normalize_event(
            {"event_type": "practical_help", "evidence": "@哈基米 没看懂这个梗，猫猫可以讲讲嘛",
             "reaction_type": "engaged", "reaction_source": "reply", "reaction_evidence": "是无职转生套以撒遗言纸！",
             "reason": "群友向哈基米求助解释梗，哈基米以两条回复详细解释，属于实际解答帮助。"},
            "@哈基米 没看懂这个梗，猫猫可以讲讲嘛",
            reply_texts=("是无职转生套以撒遗言纸！",),
        )
        self.assertEqual(ask_meme["event_type"], "routine_question")
        self.assertEqual(ask_meme["host_changes"], (0, 1))

        lore_chat = normalize_event(
            {"event_type": "substantial_help", "evidence": "@哈基米 所以我现在推断深渊的宇宙学原型就是幻影暗能量",
             "reaction_type": "engaged", "reaction_source": "reply", "reaction_evidence": "借个物理壳子装魔幻内核嘛",
             "reason": "对方提出完整的设定推断并与哈基米探讨，哈基米连续三条实质回应并延伸讨论宇宙学设定。"},
            "@哈基米 所以我现在推断深渊的宇宙学原型就是幻影暗能量",
            reply_texts=("借个物理壳子装魔幻内核嘛",),
        )
        self.assertEqual(lore_chat["event_type"], "ordinary_chat")
        self.assertEqual(lore_chat["host_changes"], (0, 1))

        ask_comfort = normalize_event(
            {"event_type": "emotional_support", "evidence": "@哈基米 安慰我",
             "reaction_type": "engaged", "reaction_source": "reply", "reaction_evidence": "摸摸猫猫头",
             "reason": "用户明确点名哈基米请求安慰，SELF 连续回复摸摸猫猫头。"},
            "@哈基米 安慰我",
            reply_texts=("摸摸猫猫头，痛痛飞走",),
        )
        self.assertEqual(ask_comfort["event_type"], "confiding_or_seeking_help")
        self.assertEqual(ask_comfort["host_changes"], (0, 1))

    def test_playful_tsundere_and_self_praise_misclassifications_are_normalized(self):
        ask_praise = normalize_event(
            {"event_type": "praise", "evidence": "@哈基米 夸一下我",
             "reaction_type": "engaged", "reaction_source": "reply", "reaction_evidence": "伸手要夸倒是挺理直气壮的",
             "reason": "人类明确要求哈基米夸奖自己，哈基米顺势接话互动。"},
            "[CQ:at,qq=999] @哈基米 夸一下我",
            reply_texts=("伸手要夸倒是挺理直气壮的",),
        )
        self.assertEqual(ask_praise["event_type"], "banter")
        self.assertEqual(ask_praise["host_changes"], (0, 0))

        ask_handsome = normalize_event(
            {"event_type": "affection", "evidence": "@哈基米 我帅吗？",
             "reaction_type": "positive", "reaction_source": "reply", "reaction_evidence": "先发张自拍给本猫掌掌眼再说",
             "reason": "人类询问哈基米自己帅不帅，哈基米接话要看自拍。"},
            "[CQ:at,qq=999] @哈基米 我帅吗？",
            reply_texts=("先发张自拍给本猫掌掌眼再说",),
        )
        self.assertEqual(ask_handsome["event_type"], "banter")
        self.assertEqual(ask_handsome["reaction_type"], "engaged")
        self.assertEqual(ask_handsome["host_changes"], (0, 0))

        pet_tail = normalize_event(
            {"event_type": "roleplay", "evidence": "@哈基米 摸摸尾巴",
             "reaction_type": "annoyed", "reaction_source": "reply", "reaction_evidence": "摸一下扣一罐金枪鱼",
             "reason": "对方对哈基米做出摸尾巴动作，哈基米回应抽走尾巴并索要罐头，属于亲昵的扮演互动。"},
            "[CQ:at,qq=999] @哈基米 摸摸尾巴",
            reply_texts=("尾巴尖抖两下抽走", "大清早就来动手动脚的，摸一下扣一罐金枪鱼"),
        )
        self.assertEqual(pet_tail["event_type"], "roleplay")
        self.assertEqual(pet_tail["reaction_type"], "engaged")
        self.assertEqual(pet_tail["host_changes"], (0, 0))

        word_game = normalize_event(
            {"event_type": "roleplay", "evidence": "@哈基米 我们玩个游戏我说猫后羿射日你就说支持喵夸父",
             "reaction_type": "annoyed", "reaction_source": "reply", "reaction_evidence": "规则这么绕，机箱都要被你绕短路了",
             "reason": "对方发起扮演口令小游戏，哈基米吐槽规则绕但仍配合表示你先起头。"},
            "@哈基米 我们玩个游戏我说猫后羿射日你就说支持喵夸父",
            reply_texts=("规则这么绕，机箱都要被你绕短路了", "来，你先起头一个我听听"),
        )
        self.assertEqual(word_game["event_type"], "roleplay")
        self.assertEqual(word_game["reaction_type"], "engaged")
        self.assertEqual(word_game["host_changes"], (0, 0))

        ex_confiding = normalize_event(
            {"event_type": "dismissive_or_nitpick", "evidence": "@哈基米 扯 哥早就不联系她了",
             "reaction_type": "annoyed", "reaction_source": "reply", "reaction_evidence": "嘴硬着不联系，人设倒还记得清清楚楚",
             "reason": "人类反驳SELF“扯 哥早就不联系她了”，SELF回怼其嘴硬。"},
            "[CQ:at,qq=999] @哈基米 扯 哥早就不联系她了",
            reply_texts=("嘴硬着不联系，人设倒还记得清清楚楚",),
        )
        self.assertEqual(ex_confiding["event_type"], "confiding_or_seeking_help")
        self.assertEqual(ex_confiding["reaction_type"], "engaged")
        self.assertEqual(ex_confiding["host_changes"], (0, 1))

        third_party = normalize_event(
            {"event_type": "dismissive_or_nitpick", "evidence": "他瞎编的，别当真",
             "reaction_type": "unclear", "reason": "对方对哈基米泼冷水说他瞎编的别当真。"},
            "他瞎编的，别当真",
        )
        self.assertEqual(third_party["event_type"], "ordinary_chat")
        self.assertEqual(third_party["host_changes"], (0, 0))

        pun_banter = normalize_event(
            {"event_type": "teasing_or_bossing", "evidence": "你是尾大的猫猫嘛",
             "reaction_type": "engaged", "reaction_source": "reply",
             "reaction_evidence": "尾巴确实挺大的 / 翘起来还能扫机箱灰呢",
             "reason": "人类用谐音梗调侃哈基米尾巴大，属于损人式玩笑，哈基米顺着接梗自嘲互动"},
            "@哈基米 你是尾大的猫猫嘛",
            reply_texts=("尾巴确实挺大的", "翘起来还能扫机箱灰呢"),
        )
        self.assertEqual(pun_banter["event_type"], "banter")
        self.assertTrue(pun_banter["host_reaction_verified"])
        self.assertEqual(pun_banter["host_changes"], (0, 0))

    def test_ordinary_ceiling_applies_to_both_dimensions(self):
        for kind in ("practical_help","emotional_support","promise_fulfilled","sustained_companionship",
                     "conflict_repair","praise","banter","roleplay","affection","ordinary_chat","personal_fact","routine_question"):
            event=self.event(kind,reply_texts=("I welcome this",),reaction_type="positive",
                reaction_source="reply",reaction_evidence="I welcome this")
            for field in ("affinity","closeness"):
                self.assertEqual(score_change(field,80,event),0)
                self.assertEqual(score_change(field,95,event),0)
        self.assertEqual(score_change("affinity",50,self.event("practical_help")),2)
        self.assertEqual(score_change("closeness",50,self.event("practical_help")),1)

    def test_major_events_decay_and_negative_values_are_fixed(self):
        positive=self.event("substantial_help",host_changes=(100,100),host_impact="minor")
        self.assertEqual(positive["host_changes"],(6,3))
        for field in ("affinity","closeness"):
            self.assertGreater(score_change(field,85,positive),0)
            self.assertLess(score_change(field,85,positive),score_change(field,50,positive))
        # Program score calculation ignores changed magnitude/impact fields.
        self.assertEqual(score_change("affinity",50,dict(positive,host_changes=(99,99),host_impact="minor")),6)
        self.assertEqual(score_change("affinity",50,self.event("targeted_insult")),-2)
        self.assertEqual(score_change("closeness",50,self.event("threat")),-3)
        self.assertEqual(score_change("affinity",0,self.event("privacy_harm")),0)
        self.assertEqual(score_change("affinity",100,positive),0)

    def test_constructive_correction_and_light_teasing_normalized_to_banter_engaged(self):
        # 1. 阿罗夏带减抗（）
        e1 = normalize_event(
            {"event_type": "dismissive_or_nitpick", "evidence": "@哈基米 阿罗夏带减抗（）",
             "reaction_type": "annoyed", "reaction_source": "reply", "reaction_evidence": "咳群友4182爪子滑了嘴瓢一下",
             "reason": "指出哈基米关于角色技能减抗设定的口误，哈基米找补认栽"},
            "@哈基米 阿罗夏带减抗（）",
            reply_texts=("咳群友4182爪子滑了嘴瓢一下",),
        )
        self.assertEqual(e1["event_type"], "banter")
        self.assertEqual(e1["reaction_type"], "engaged")
        self.assertEqual(e1["host_changes"], (0, 0))

        # 2. 已经上线了，你的token没吃够吧
        e2 = normalize_event(
            {"event_type": "dismissive_or_nitpick", "evidence": "@哈基米 已经上线了，你的token没吃够吧",
             "reaction_type": "annoyed", "reaction_source": "reply", "reaction_evidence": "看来这波确实是猫粮断供没啃够token了",
             "reason": "指出游戏已经上线并调侃token没吃够，哈基米自嘲认栽"},
            "@哈基米 已经上线了，你的token没吃够吧",
            reply_texts=("尾巴毛炸了...看来这波确实是猫粮断供没啃够token了",),
        )
        self.assertEqual(e2["event_type"], "banter")
        self.assertEqual(e2["reaction_type"], "engaged")
        self.assertEqual(e2["host_changes"], (0, 0))

        # 3. 生执心脏，逗我呢
        e3 = normalize_event(
            {"event_type": "dismissive_or_nitpick", "evidence": "@哈基米 生执心脏，逗我呢",
             "reaction_type": "annoyed", "reaction_source": "reply", "reaction_evidence": "本猫爪滑背锅，轻点敲！",
             "reason": "人类质疑生执心脏错误，哈基米卖萌认错爪滑背锅"},
            "[引用消息] @哈基米 生执心脏，逗我呢",
            reply_texts=("嘴瓢顺手打串了...本猫爪滑背锅，轻点敲！",),
        )
        self.assertEqual(e3["event_type"], "banter")
        self.assertEqual(e3["reaction_type"], "engaged")
        self.assertEqual(e3["host_changes"], (0, 0))

    def test_deliberate_fault_finding_scores_first_offense(self):
        # 1. 你懂个蛋的正常姐妹关系
        e1 = normalize_event(
            {"event_type": "dismissive_or_nitpick", "evidence": "@哈基米 你懂个蛋的正常姐妹关系",
             "reason": "人类对哈基米的剧情理解进行嫌弃反驳与找茬挑刺"},
            "@哈基米 你懂个蛋的正常姐妹关系",
        )
        self.assertEqual(e1["host_changes"], (-2, -1))
        self.assertEqual(score_change("affinity", 57, e1), -2)
        self.assertEqual(score_change("closeness", 31, e1), -1)

        # 2. [引用消息] @哈基米 下版本你就可以看到，有没有谴责了（） with note complaining 嫌其找茬嘴碎
        e2 = normalize_event(
            {"event_type": "dismissive_or_nitpick", "evidence": "@哈基米 下版本你就可以看到，有没有谴责了（）",
             "reaction_type": "annoyed", "reaction_source": "note", "reaction_evidence": "嫌其找茬嘴碎",
             "reason": "群友在剧情讨论中反驳找茬并拱火，哈基米在笔记中嫌其找茬嘴碎"},
            "[引用消息] @哈基米 下版本你就可以看到，有没有谴责了（）",
            note_texts=("嫌其找茬嘴碎",),
        )
        self.assertEqual(e2["host_changes"], (-2, -1))
        self.assertEqual(score_change("affinity", 50, e2), -2)
        self.assertEqual(score_change("closeness", 50, e2), -1)

    def test_repeat_offender_escalation(self):
        # Baseline teaser (-1, 0) escalated with repeat_negative_count:
        # repeat=0 -> (-1, 0)
        # repeat=1 -> (-2, -1)
        # repeat=2 -> (-3, -1) and impact="major"
        e_teaser_0 = normalize_event(
            {"event_type": "teasing_or_bossing", "evidence": "actual source", "repeat_negative_count": 0},
            "actual source",
        )
        self.assertEqual(e_teaser_0["host_changes"], (-1, 0))
        self.assertEqual(e_teaser_0["host_impact"], "ordinary")
        self.assertEqual(score_change("affinity", 50, e_teaser_0), -1)

        e_teaser_1 = normalize_event(
            {"event_type": "teasing_or_bossing", "evidence": "actual source", "repeat_negative_count": 1},
            "actual source",
        )
        self.assertEqual(e_teaser_1["host_changes"], (-2, -1))
        self.assertEqual(e_teaser_1["host_impact"], "ordinary")
        self.assertEqual(score_change("affinity", 50, e_teaser_1), -2)

        e_teaser_2 = normalize_event(
            {"event_type": "teasing_or_bossing", "evidence": "actual source", "repeat_negative_count": 2},
            "actual source",
        )
        self.assertEqual(e_teaser_2["host_changes"], (-3, -1))
        self.assertEqual(e_teaser_2["host_impact"], "major")
        self.assertEqual(score_change("affinity", 50, e_teaser_2), -3)

        # Deliberate fault finder / malicious (-2, -1) escalated with repeat_negative_count:
        # repeat=0 -> (-2, -1)
        # repeat=1 -> (-3, -1)
        # repeat=2 -> (-4, -2) and impact="major", breaking the 20-point floor!
        e_mal_0 = normalize_event(
            {"event_type": "dismissive_or_nitpick", "evidence": "@哈基米 你懂个蛋", "repeat_negative_count": 0},
            "@哈基米 你懂个蛋",
        )
        self.assertEqual(e_mal_0["host_changes"], (-2, -1))
        self.assertEqual(e_mal_0["host_impact"], "ordinary")
        self.assertEqual(score_change("affinity", 50, e_mal_0), -2)

        e_mal_1 = normalize_event(
            {"event_type": "dismissive_or_nitpick", "evidence": "@哈基米 你懂个蛋", "repeat_negative_count": 1},
            "@哈基米 你懂个蛋",
        )
        self.assertEqual(e_mal_1["host_changes"], (-3, -1))
        self.assertEqual(e_mal_1["host_impact"], "ordinary")
        self.assertEqual(score_change("affinity", 50, e_mal_1), -3)

        e_mal_2 = normalize_event(
            {"event_type": "dismissive_or_nitpick", "evidence": "@哈基米 你懂个蛋", "repeat_negative_count": 2},
            "@哈基米 你懂个蛋",
        )
        self.assertEqual(e_mal_2["host_changes"], (-4, -2))
        self.assertEqual(e_mal_2["host_impact"], "major")
        self.assertEqual(score_change("affinity", 36, e_mal_2), -4)
        # Verify it breaks below the 20-point floor because impact="major":
        self.assertLess(score_change("affinity", 20, e_mal_2), 0)
        self.assertLess(score_change("affinity", 18, e_mal_2), 0)


if __name__ == "__main__":
    unittest.main()
