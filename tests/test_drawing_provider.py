import base64
from contextlib import contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from hakimi.tools import drawing
from hakimi.tools.drawing_settings import DEFAULT_ENDPOINT, DEFAULT_MODEL


KEY = "fake-drawing-key-for-unit-tests"
BRIEF = "我想画一只在暴雨后等群友317a的小猫：它把唯群友b331把干伞放在对面的椅子上。"


def image_bytes(image_format):
    out = BytesIO()
    with Image.new("RGB", (6, 7), (120, 10, 90)) as img:
        img.save(out, format=image_format)
    return out.getvalue()


def inline(image, mime="image/png"):
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(image).decode("ascii")}}


class FakeResponse(BytesIO):
    def __init__(self, body, *, status=200, encoding="identity", length=None):
        super().__init__(body)
        self.status = status
        self.encoding = encoding
        self.length = len(body) if length is None else length

    def getheader(self, name, default=None):
        return self.encoding if name.lower() == "content-encoding" else default


class DrawingProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def enable(self, **extra):
        return drawing.save_settings(self.base, {"enabled": True, "api_key": KEY, **extra})

    def mock_connection(self, document, *, status=200, error=None):
        requests = []
        connections = []
        body = document if isinstance(document, bytes) else json.dumps(document).encode()

        class FakeConnection:
            def request(self, method, path, *, body, headers):
                requests.append((method, path, json.loads(body), headers))
                if error:
                    raise error

            def getresponse(self):
                return FakeResponse(body, status=status)

        @contextmanager
        def open_connection(endpoint, deadline):
            connections.append(endpoint)
            yield FakeConnection()

        return patch.object(drawing, "_https_connection", open_connection), requests, connections

    def test_settings_are_private_and_blank_retains_key(self):
        initial = drawing.public_settings(self.base)
        self.assertFalse(initial["enabled"])
        self.assertEqual(initial["endpoint"], DEFAULT_ENDPOINT)
        self.assertEqual(initial["model"], DEFAULT_MODEL)
        public = self.enable(daily_usd_limit="0.35", reserve_usd="0.05")
        self.assertTrue(public["has_api_key"])
        self.assertNotIn("api_key", public)
        self.assertNotIn(KEY, json.dumps(public))
        path = self.base / "state" / "drawing_provider.json"
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        updated = drawing.save_settings(self.base, {"api_key": " ", "model": "models/image-test"})
        self.assertTrue(updated["has_api_key"])
        self.assertEqual(updated["reserve_usd"], "0.05")
        self.assertEqual(updated["daily_usd_limit"], "0.35")
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["api_key"], KEY)
        self.assertEqual(on_disk["model"], "image-test")

    def test_provider_origin_change_requires_explicit_key(self):
        self.enable()
        with self.assertRaisesRegex(drawing.DrawingError, "重新输入密钥"):
            drawing.save_settings(self.base, {"endpoint": "https://images.example.com:1443/gemini"})
        self.assertEqual(drawing.public_settings(self.base)["endpoint"], DEFAULT_ENDPOINT)
        updated = drawing.save_settings(self.base, {"endpoint": "https://images.example.com:1443/gemini", "api_key": KEY})
        self.assertEqual(updated["endpoint"], "https://images.example.com:1443/gemini")

    def test_bad_endpoints_are_ignored_without_echoing_secrets(self):
        for url in (f"https://api.example.org/?key={KEY}", f"https://{KEY}@api.example.org/",
                    "https://127.0.0.1/v1beta", "https://[::ffff:192.168.2.5]/v1beta",
                    "https://[64:ff9b::7f00:1]/v1beta", "https://224.0.0.1/v1beta",
                    "http://api.example.org", "https://api.example.org/../v1beta",
                    "https://api.example.org:0/v1beta"):
            with self.subTest(url=url), self.assertRaises(drawing.DrawingError) as raised:
                self.enable(endpoint=url)
            self.assertNotIn(KEY, str(raised.exception))
        with self.assertRaises(drawing.DrawingError) as raised:
            self.enable(endpoint="https://api.example.org/v1beta/models/other:generateContent")
        self.assertEqual(raised.exception.code, "drawing_model_path")

    def test_disabled_does_not_use_another_google_credential(self):
        with patch.dict(os.environ, {"GOOGLE_API_KEY": KEY, "GEMINI_API_KEY": KEY}), \
                patch.object(drawing, "_https_connection") as network, \
                self.assertRaises(drawing.DrawingError) as raised:
            drawing.generate(self.base, BRIEF)
        self.assertEqual(raised.exception.code, "drawing_disabled")
        network.assert_not_called()

    def test_rejects_entire_dns_answer_when_one_address_is_private(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.1.2", 443))]
        with patch.object(socket, "getaddrinfo", return_value=addresses), \
                self.assertRaises(drawing.DrawingError) as raised:
            drawing._public_addresses("api.example.org", 443)
        self.assertEqual(raised.exception.code, "drawing_endpoint")

    def test_native_request_contains_only_prefix_and_her_creative_brief(self):
        self.enable(endpoint="https://images.example.com/gemini/v1beta/", model="image-test")
        image = image_bytes("PNG")
        thoughts = inline(b"secret interim image") | {"thought": True}
        doc = {"candidates": [{"finishReason": "STOP", "content": {"parts": [thoughts, inline(image)]}}],
               "usageMetadata": {"actualCostUsd": "0.04"}}
        network, requests, connections = self.mock_connection(doc)
        with network, patch.object(drawing, "reserve", return_value="reservation") as reserve, \
                patch.object(drawing, "finish") as finish:
            result = drawing.generate(self.base, BRIEF)
        self.assertEqual(result, drawing.GeneratedImage(image, "image/png"))
        self.assertEqual(len(connections), 1)
        self.assertEqual(len(requests), 1)
        method, path, sent, headers = requests[0]
        self.assertEqual((method, path), ("POST", "/gemini/v1beta/models/image-test:generateContent"))
        self.assertEqual(headers["x-goog-api-key"], KEY)
        self.assertNotIn(KEY, json.dumps(sent))
        self.assertEqual(sent["generationConfig"], {"responseModalities": ["TEXT", "IMAGE"], "candidateCount": 1})
        self.assertEqual(sent["contents"], [{"role": "user", "parts": [{"text": drawing.CREATIVE_PREFIX + BRIEF}]}])
        reserve.assert_called_once()
        finish.assert_called_once_with(self.base, "reservation", doc, rejected=False)

    def test_selects_only_supported_verified_final_image_bytes(self):
        for image_format, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
            with self.subTest(image_format=image_format):
                image = image_bytes(image_format)
                self.assertEqual(drawing._decode_image(inline(image, mime)["inlineData"]), drawing.GeneratedImage(image, mime))
                self.assertIsNone(drawing._decode_image(inline(image + b"appended", mime)["inlineData"]))
                self.assertIsNone(drawing._decode_image(inline(image[:len(image)//2], mime)["inlineData"]))
        png = image_bytes("PNG")
        self.assertIsNone(drawing._decode_image(inline(png, "image/jpeg")["inlineData"]))
        self.assertIsNone(drawing._decode_image({"mimeType": "image/png", "data": "!not base64!"}))
        with patch.object(drawing, "_MAX_IMAGE_EDGE", 5):
            self.assertIsNone(drawing._decode_image(inline(png)["inlineData"]))

    def test_error_body_never_echoes_and_http_is_not_retried(self):
        self.enable()
        for status, code, rejected in ((302, "drawing_provider", False), (401, "drawing_auth", True),
                                       (429, "drawing_provider_limit", True), (503, "drawing_provider", False)):
            with self.subTest(status=status):
                network, requests, _ = self.mock_connection({"error": f"{KEY} {BRIEF}"}, status=status)
                with network, patch.object(drawing, "reserve", return_value="reservation"), \
                        patch.object(drawing, "finish") as finish, self.assertRaises(drawing.DrawingError) as raised:
                    drawing.generate(self.base, BRIEF)
                self.assertEqual(len(requests), 1)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn(KEY, str(raised.exception))
                self.assertNotIn(BRIEF, str(raised.exception))
                finish.assert_called_once_with(self.base, "reservation", None, rejected=rejected)

    def test_provider_success_is_reconciled_even_if_image_is_unusable(self):
        self.enable()
        doc = {"candidates": [{"content": {"parts": [inline(b"fake image")]}}],
               "usageMetadata": {"actualCostUsd": "0.04"}}
        network, _, _ = self.mock_connection(doc)
        with network, patch.object(drawing, "reserve", return_value="reservation"), \
                patch.object(drawing, "finish") as finish, self.assertRaises(drawing.DrawingError) as raised:
            drawing.generate(self.base, BRIEF)
        self.assertEqual(raised.exception.code, "drawing_invalid_image")
        finish.assert_called_once_with(self.base, "reservation", doc, rejected=False)

    def test_cap_stops_post_and_unobserved_request_keeps_pending_receipt(self):
        self.enable(daily_usd_limit="0.05", reserve_usd="0.05")
        network, requests, _ = self.mock_connection({}, error=OSError(KEY))
        with network:
            with self.assertRaises(drawing.DrawingError) as raised:
                drawing.generate(self.base, BRIEF)
            self.assertEqual(raised.exception.code, "drawing_connection")
            self.assertNotIn(KEY, str(raised.exception))
            with self.assertRaises(drawing.DrawingError) as raised:
                drawing.generate(self.base, BRIEF)
            self.assertEqual(raised.exception.code, "drawing_daily_limit")
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
