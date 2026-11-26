"""Offline checks for compatibility across the OneBot transport boundary."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from PIL import Image

from test_delivery_guard import OfflineEventLoop


class OneBotMediaTests(unittest.TestCase):
    def test_local_napcat_gif_keeps_original_and_reuses_cached_jpeg(self):
        from hakimi.bridge import bridge_daemon as bridge

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source = root / "napcat.gif"
            Image.new("RGB", (32, 24), "red").save(source)
            with patch.object(bridge, "IMG_CACHE_DIR", root), patch.object(
                bridge, "onebot_http_post", return_value={"data": {"file": str(source)}},
            ) as post:
                jpeg = Path(bridge.cache_qq_image_to_jpg("file-id", "", "group:12/34"))
                self.assertEqual(jpeg.name, "img_group_12_34.jpg")
                self.assertEqual(jpeg.with_suffix(".gif").read_bytes(), source.read_bytes())
                with Image.open(jpeg) as cached:
                    self.assertEqual((cached.format, cached.mode, cached.size), ("JPEG", "RGB", (32, 24)))
                self.assertEqual(bridge.cache_qq_image_to_jpg("file-id", "", "group:12/34"), jpeg.as_posix())
                post.assert_called_once_with("get_image", {"file": "file-id"}, timeout=6.0)

    def test_remote_quote_uses_injected_image_provider_in_same_session(self):
        from hakimi.bridge import bridge_daemon as bridge

        quoted = {"data": {"group_id": 12, "sender": {"card": "群友"},
                           "raw_message": "[CQ:reply,id=8][CQ:image,file=quoted]"}}
        images = []
        with (
            patch.object(bridge, "onebot_http_post", return_value=quoted) as post,
            patch.object(bridge, "cache_qq_image_to_jpg", return_value="cached.jpg") as cache,
            patch.object(bridge, "register_cached_image") as register,
        ):
            result = bridge.format_clean_message("[CQ:reply,id=7]", "99", 1, {}, images, "group:12")

        self.assertIn("群友", result)
        self.assertIn("cached.jpg", result)
        self.assertEqual(images, ["cached.jpg"])
        self.assertEqual(post.call_args_list, [call("get_msg", {"message_id": 7}, timeout=4.0)])
        cache.assert_called_once_with("quoted", "", "group:12_7_1")
        register.assert_called_once_with("group:12", "cached.jpg")


class OneBotWebSocketTests(unittest.IsolatedAsyncioTestCase):
    loop_factory = OfflineEventLoop

    async def test_handler_separates_acknowledgments_from_events_and_cleans_up_on_disconnect(self):
        from hakimi.bridge import bridge_daemon as bridge

        ack = {"echo": "first", "status": "ok", "retcode": 0}
        event = {"post_type": "message", "message_id": 7}

        class Socket:
            async def __aiter__(self):
                for frame in (ack, [], {"post_type": "notice"}, event):
                    yield json.dumps(frame)

        owner = Socket()
        other = Socket()
        loop = asyncio.get_running_loop()
        acknowledged, disconnected, unrelated = (loop.create_future() for _ in range(3))
        pending = {
            "first": (acknowledged, owner),
            "second": (disconnected, owner),
            "third": (unrelated, other),
        }
        queue = asyncio.Queue()
        clients = {}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            status = Path(directory) / "online.json"
            status.touch()
            with (
                patch.object(bridge, "ACTIVE_WS_CLIENTS", clients),
                patch.object(bridge, "PENDING_ACKS", pending),
                patch.object(bridge, "INCOMING_EVENTS", queue),
                patch.object(bridge, "ONLINE_STATUS_FILE", status),
            ):
                await bridge.onebot_ws_handler(owner)
            self.assertFalse(status.exists())
        self.assertEqual(acknowledged.result(), ack)
        self.assertEqual(disconnected.result(), {"status": "unknown"})
        self.assertFalse(unrelated.done())
        unrelated.cancel()
        self.assertEqual(queue.get_nowait(), event)
        self.assertTrue(queue.empty())
        self.assertEqual(clients, {})
