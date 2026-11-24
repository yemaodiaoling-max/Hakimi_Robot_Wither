import unittest
from hakimi.prompt.prompt_context import is_admin_turn, admin_interaction_guidance, render_prompt, render_vertex_prompt


class TestAdminDynamicGuidance(unittest.TestCase):
    def test_regular_user_zero_token(self):
        regular_target = {"user_id": "811610", "nickname": "普通群友"}
        session = "group:12345"
        self.assertFalse(is_admin_turn(regular_target, session))
        self.assertEqual(admin_interaction_guidance(regular_target, session), "")

        payload = {
            "session_key": session,
            "wake_mode": "群聊@回复",
            "recent_context_messages": [
                {"message_id": "1", "user_id": "811610", "nickname": "普通群友", "text": "你好呀"}
            ]
        }
        prompt = render_prompt(payload)
        self.assertNotIn("管理员指令服从", prompt)
        self.assertNotIn("admin_privilege", prompt)

        v_prompt = render_vertex_prompt(payload)
        self.assertNotIn("管理员指令服从", v_prompt)
        self.assertNotIn("admin_privilege", v_prompt)

    def test_admin_user_dynamic_injection(self):
        for admin_target in [
            {"user_id": "8549864100", "nickname": "主人"},
            {"user_id": "8112866014149232024", "nickname": "主人"},
            {"user_id": "random_sub_id", "nickname": "主人"},
        ]:
            session = "group:12345"
            self.assertTrue(is_admin_turn(admin_target, session))
            guidance = admin_interaction_guidance(admin_target, session)
            self.assertIn("管理员指令服从", guidance)
            self.assertIn("主人", guidance)

            payload = {
                "session_key": session,
                "wake_mode": "群聊@回复",
                "recent_context_messages": [
                    {"message_id": "2", "user_id": admin_target["user_id"], "nickname": admin_target["nickname"], "text": "哈基米给我倒杯水"}
                ]
            }
            prompt = render_prompt(payload)
            self.assertIn("管理员指令服从", prompt)

            v_prompt = render_vertex_prompt(payload)
            self.assertIn("管理员指令服从", v_prompt)
            self.assertIn("admin_privilege", v_prompt)


if __name__ == "__main__":
    unittest.main()
