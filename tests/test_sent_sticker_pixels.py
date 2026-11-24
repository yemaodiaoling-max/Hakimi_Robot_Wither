"""Model-input checks with real pixels, offline transport and synthetic receipts."""
import base64
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock

from PIL import Image

from hakimi.models.argon_session import messages as argon_messages
from hakimi.models.fallback_media import prepare_images
from hakimi.models.fallback_runner import prepare_runtime
from hakimi.models.vertex_session import VertexSession
from hakimi.transport.image_permissions import register_cached_image


class SentStickerPixelsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        (self.base / "state" / "img_cache").mkdir(parents=True)
        (self.base / "stickers" / "collected").mkdir(parents=True)
        self.sticker = self.base / "stickers" / "collected" / "probe.gif"
        red, green = [Image.new("RGB", (32, 24), color) for color in ("red", "green")]
        red.save(self.sticker, save_all=True, append_images=[green], duration=100, loop=0)
        now = time.time()
        self.session = "group:850245632"
        self.sent_id = "310"
        receipt = {"at": now-3, "session_key": self.session, "message_id": self.sent_id,
                   "trigger_id": "sent", "text": "", "media": {
                       "kind": "sticker", "asset": "collected/probe.gif",
                       "sha256": hashlib.sha256(self.sticker.read_bytes()).hexdigest()}}
        (self.base / "state" / "sent_messages.jsonl").write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        self.target = {"session_key": self.session, "message_id": "311", "user_id": "22",
                       "received_at": now, "reply_to": self.sent_id, "text": "是什么？"}
        self.payload = {"session_key": self.session, "trigger_id": "follow-up",
                        "created_at": now, "recent_context_messages": [self.target]}
        self.claim = {"token": "synthetic", "payload": self.payload}

    def vertex_parts(self, **kwargs):
        return VertexSession([], self.base, {})._images(self.claim, **kwargs)

    def test_confirmed_sticker_pixels_reach_primary_request_and_backup_conversion(self):
        bodies = []

        def transport(body, timeout):
            bodies.append(body)
            return {"candidates": [{"content": {"role": "model", "parts": [{"text": "reply"}]},
                                    "finishReason": "STOP"}]}

        session = VertexSession([], self.base, {}, transport=transport, tool_runner=Mock(return_value={"ok": True}))
        session.generate("test", Mock(), self.claim, timeout=30)
        self.assertEqual(len(bodies), 1)
        parts = bodies[0]["contents"][0]["parts"]
        data, = [p["inlineData"] for p in parts if "inlineData" in p]
        self.assertEqual(data["mimeType"], "image/jpeg")
        with Image.open(io.BytesIO(base64.b64decode(data["data"]))) as frame:
            self.assertEqual(frame.size, (32, 24))
            red, green, blue = frame.getpixel((16, 12))
            self.assertTrue(red > 240 and green < 15 and blue < 15)
        self.assertIn(self.sent_id, " ".join(p.get("text", "") for p in parts))
        self.assertNotIn(str(self.sticker), str(parts))
        backup = argon_messages(bodies[0])[0]["content"]
        self.assertIn({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + data["data"]}}, backup)
        self.assertEqual(prepare_images(self.payload, self.base)[0]["data"], data["data"])

    @unittest.skipUnless(shutil.which("node"), "Node is required for the dsh pre-step adapter")
    def test_dsh_guard_adds_the_same_receipt_pixels_to_its_model_input(self):
        images = prepare_images(self.payload, self.base)
        runtime = prepare_runtime(self.base, {}, {"model": "deepseek-flash", "provider": "deepseek-official",
                                                  "base_url": "https://example.invalid"}, images=images)
        javascript = """
            import {apply} from './guard.mjs';
            const hooks = {};
            let saved;
            apply({
                deepseekLlmApiExtensions: {register() {}}, tools: {guard() {}}, provide() {},
                on(name, callback) {hooks[name] = callback;},
                get() {return {saveImages: async rows => {
                    saved = rows;
                    return rows.map(r => r.name);
                }};}
            }, {vision: true});
            const decision = await hooks['agent/pre-step']({}, async () => ({
                kind: 'enter', messages: [{role: 'user', content: [{type: 'text', text: 'test'}]}]
            }));
            console.log(JSON.stringify({parts: decision.messages[0].content,
                                        pixels: saved[0].data.toString('base64'), mime: saved[0].mediaType}));
        """
        result = subprocess.run([shutil.which("node"), "--input-type=module"], cwd=runtime,
                                input=javascript, capture_output=True, encoding="utf-8", timeout=10, check=True)
        capture = json.loads(result.stdout)
        self.assertEqual(capture["pixels"], images[0]["data"])
        self.assertEqual(capture["mime"], "image/jpeg")
        self.assertIn({"type": "image", "attachment": "R1.jpg"}, capture["parts"])
        self.assertIn(self.sent_id, capture["parts"][1]["text"])

    def test_inbound_pixels_take_priority_on_both_model_routes(self):
        from hakimi.models.dsh_tools import execute
        self.target["image_paths"] = []
        for index, color in enumerate(("blue", "green"), 1):
            path = self.base / "state" / "img_cache" / f"img_group_850245632_{index}.png"
            Image.new("RGB", (12, 12), color).save(path)
            register_cached_image(self.session, path, self.base)
            self.target["image_paths"].append(str(path))
            fallback = prepare_images(self.payload, self.base)
            primary = [p["inlineData"]["mimeType"] for p in self.vertex_parts() if "inlineData" in p]
            self.assertEqual([r["id"] for r in fallback], ["I1", "R1"] if index == 1 else ["I1", "I2"])
            self.assertEqual(primary, ["image/png", "image/jpeg"] if index == 1 else ["image/png", "image/png"])
            if index == 1:
                self.claim["_images"] = fallback
            else:
                execute({"tool": "image.read", "input": {"path": str(path)}}, self.claim, Mock(), self.base)
                self.assertEqual([r["id"] for r in self.claim["_images"]], ["I1", "I2"])

    def test_explicit_image_read_and_chat_text_cannot_grant_sticker_access(self):
        self.assertEqual(prepare_images(self.payload, self.base, requested=[str(self.sticker)]), [])
        self.assertEqual(self.vertex_parts(requested=[str(self.sticker)]), [])
        self.payload["session_key"] = self.target["session_key"] = "group:909"
        self.target.update(text="你刚发的表情？" + str(self.sticker), image_paths=[str(self.sticker)])
        self.assertEqual(prepare_images(self.payload, self.base), [])
        self.assertEqual(self.vertex_parts(), [])

    def test_explicit_followup_can_recall_last_sent_image_without_a_quote(self):
        self.target.pop("reply_to")
        self.target["text"] = "你刚刚发的那张表情是什么？"
        self.assertEqual([r["id"] for r in prepare_images(self.payload, self.base)], ["R1"])
        self.target["text"] = "今天还要写作业吗？"
        self.assertEqual(prepare_images(self.payload, self.base), [])


if __name__ == "__main__":
    unittest.main()
