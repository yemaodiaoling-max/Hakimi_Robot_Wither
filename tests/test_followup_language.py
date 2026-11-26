import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hakimi.bridge import followup_native
from hakimi.core.context_window import select_context


class FollowupLanguage(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("hakimi.bridge._followup_rules"), "optional C++ module")
    def test_contextual_pairs(self):
        cases = [
            ("帮我写一下活动邀请", "用英文，语气轻松点", 1),
            ("帮我写一下活动邀请", "英文", 1),
            ("帮我写一下活动邀请", "只给结论", 1),
            ("帮我写一下活动邀请", "不要加标题", 1),
            ("帮我写一下活动邀请", "不是会议邀请，而是生日邀请", 1),
            ("帮我写一下活动邀请", "写错了，是活动通知", 1),
            ("安排一下会议\n跟产品组一起", "明天下午3点", 1),
            ("提醒我去买菜", "19:30", 1),
            ("看下纽约天气", "周末", 1),
            ("帮我查一下退款政策", "刚才那个，是在官网买的", 1),
            ("帮我推荐咖啡店", "3家", 1),
            ("帮我推荐咖啡店", "3个", 1),
            ("写个邮件", " 控制在 200 字以内 ", 1),
            ("解释下这个算法", "那如果数组为空呢？", 1),
            ("Draft an invite", "IN ENGLISH", 1),
            ("Draft an invite", "keep it casual", 1),
            ("Schedule a conference", "at 3:30 pm", 1),
            ("帮我准备会议议程", "会议要订什么场地？", 0),
            ("帮我写一段 Python", "Python 的解释器怎么装？", 0),
            ("帮我写一段 Python", "帮我查一下 Python 版本", 0),
            ("帮我写一段 Python", "顺便帮我查官网", 0),
            ("帮我写一段 Python", "另外一个问题，可以用 Go 吗？", 0),
            ("写个活动通知", "用英文怎么说‘生日快乐’", 0),
            ("给我解释下行星引力", "明天", 0),
            ("你好", "不对，是周末", 0),
            ("[图片]", "补充一下，这张图", 0),
            ("帮我准备会议议程", " ", 0),
            ("帮我准备会议议程", "[CQ:image,file=3.jpg]", 0),
            ("help plan a conference", "what does conference mean?", 0),
            ("help plan a conference", "another question: book a room?", 0),
            ("Draft an invite", "Actually, write a budget report", 0),
            ("帮我查一下天气", "我刚才买了一只猫", 0),
            ("推荐两家餐厅", "人均一百以内", 1),
            ("看看北京天气", "就明天下午", 1),
            ("帮我看一下这段报错", "我用的是Windows 11", 1),
            ("帮我写通知", "用中文怎么说谢谢", 0),
            ("帮我写通知", "我用的是Windows 11", 0),
            ("看看北京天气", "人均一百以内", 0),
            ("看看北京天气", "明天下午可以帮我写个通知吗？", 0),
            ("定个旅行计划", "带着老人和小孩", 1),
            ("解释引力", "带着老人和小孩", 0),
            ("给个能批量改名的程序", "别用第三方库", 1),
            ("今天有多冷", "别用第三方库", 0),
            ("查一下天气", "杭州的", 1),
            ("解释下引力", "杭州的", 0),
            ("帮选电脑", "预算五千", 1),
            ("分析这段歌词", "预算五千", 0),
            ("提醒我明天给物业打电话", "刚说错了是后天", 1),
            ("推荐餐厅", "最好离地铁近", 1),
            ("写一份 Python 练习题", "最好离地铁近", 0),
            ("推荐餐厅", "餐厅的消防验收怎么申请？", 0),
            ("查一下天气", "杭州的房租多少钱？", 0),
            ("帮选电脑", "电脑坏了，帮我预约维修", 0),
            ("安排行程", "带着孩子能不能去办护照", 0),
            ("plan a trip", "with kids", 1),
            ("recommend a hotel", "preferably near the subway", 1),
            ("查杭州天气", "绍兴的", 1),
            ("看看这两天气温", "张家界的", 1),
            ("帮我选电脑", "最多五千", 1),
            ("想买个冰箱", "两千以内", 1),
            ("准备个秋游行程", "带一位老人", 1),
            ("推荐餐厅", "地铁口附近", 1),
            ("写个 Python 脚本", "标准库就行", 1),
            ("写个图表程序", "框架用现成的就行", 1),
            ("查北京天气", "北京天气对房价有什么影响？", 0),
            ("帮我查会议室", "会议室的门锁坏了，帮我叫维修", 0),
            ("解释一下时间膨胀", "人均四十", 0),
            ("查查看错误日志", "i am using ubuntu 24", 1),
            ("帮我解释概念", "绍兴的", 0),
            ("帮我写个天气脚本", "绍兴的", 0),
            ("查杭州天气", "红色的", 0),
            ("帮我查查天气", "最多五千", 0),
            ("帮我选电脑", "五千", 0),
            ("写个新闻概要", "带一位老人", 0),
            ("准备个秋游行程", "带一位老人去看医生", 0),
            ("整理下这段文字", "地铁口附近", 0),
            ("查一下天气", "标准库就行", 0),
            ("写个 Python 脚本", "标准库怎么读就行吗", 0),
            ("帮我画只小猫", "算了，别画了", 1),
            ("帮我画只小猫", "不要画了，只说文字", 1),
            ("帮我画只小猫", "背景用蓝色", 1),
            ("帮我画只小猫", "水彩风格", 1),
            ("帮我写个通知", "算了，别画了", 0),
            ("帮我写个通知", "不要画了，只说文字", 0),
            ("帮我写个通知", "背景用蓝色", 0),
            ("帮我写个通知", "水彩风格", 0),
            ("帮我画只小猫", "用水彩画怎么入门？", 0),
        ]
        self.assertEqual("hakimi.bridge._followup_rules", followup_native.is_continuation.__module__)
        for previous, incoming, expected in cases:
            with self.subTest(previous=previous, incoming=incoming):
                self.assertIs(followup_native.is_continuation(previous, incoming), bool(expected))

    def test_portable_fallback_preserves_explicit_corrections(self):
        self.assertTrue(followup_native._explicit_followup("帮我安排会议", "打错了，周五下午"))
        self.assertFalse(followup_native._explicit_followup("帮我安排会议", "还有个独立问题"))

    def test_long_chain_stays_visible_across_unrelated_chat(self):
        rows = [dict(session_key="group:42", message_id=str(i), user_id="2", text="群聊",
                     received_at=1000 + i) for i in range(70)]
        sources = [str(i) for i in (1, 8, 14, 26, 39, 62)]
        for mid in sources:
            rows[int(mid)].update(user_id="3", text="补充条件" + mid)
        selected, target = select_context(dict(session_key="group:42", merged_followup=True,
                                               recent_context_messages=rows, reply_target_message_id="62",
                                               pending_explicit_question_ids=sources))
        self.assertEqual(target["message_id"], "62")
        self.assertEqual([row["message_id"] for row in selected if row["user_id"] == "3"], sources)

    def test_both_prompt_routes_keep_original_request_and_correction(self):
        from hakimi.prompt.prompt_context import render_prompt

        texts = ["帮我写一段关于月食的介绍", "更正一下，目标读者是小学生"]
        rows = [dict(session_key="group:42", message_id=str(i), user_id="9", text=text,
                     received_at=1000 + i) for i, text in enumerate(texts, 1)]
        turn = dict(session_key="group:42", merged_followup=True, recent_context_messages=rows,
                    pending_explicit_question_ids=["1", "2"], reply_target_message_id="2",
                    wake_mode="即时唤醒(被@/私聊)")
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "state").mkdir()
            (base / "style_guide.md").write_text("测试用", encoding="utf8")
            for native in (False, True):
                with self.subTest(native=native):
                    prompt = render_prompt(turn, base=base, native=native)
                    for text in texts:
                        self.assertIn(text, prompt)
                    self.assertIn("合起来回答一次", prompt)
