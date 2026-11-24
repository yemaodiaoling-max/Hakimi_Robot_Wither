"""HTTP tests for the QQ console's drawing settings."""

from http.client import HTTPResponse
from io import BytesIO
import json
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hakimi.console.memory_console import ConsoleHandler


class MemorySocket:
    def __init__(self, body):
        self.incoming = BytesIO(body)
        self.outgoing = BytesIO()

    def makefile(self, mode, *args):
        return self.incoming

    def sendall(self, data):
        self.outgoing.write(data)


class DrawingSettingsConsoleTests(unittest.TestCase):
    token = "synthetic-console-token-for-tests-only"
    api_key = "synthetic-drawing-key-for-tests-only"
    host = "localhost:18871"  # Simulate a browser using an SSH forward.
    origin = "http://" + host

    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.server = SimpleNamespace(base=state.name, token=self.token)

    def request(self, method, path, *, body=None, headers=None):
        body = (body or "").encode()
        headers = {"Host": self.host, "Content-Length": len(body), **(headers or {})}
        request = [f"{method} {path} HTTP/1.1", *(f"{key}: {value}" for key, value in headers.items())]
        connection = MemorySocket(("\r\n".join(request) + "\r\n\r\n").encode() + body)
        ConsoleHandler(connection, ("127.0.0.1", 12345), self.server)
        with HTTPResponse(MemorySocket(connection.outgoing.getvalue())) as response:
            response.begin()
            return response.status, dict(response.getheaders()), response.read()

    def settings(self, method="GET", data=None, *, authenticated=True, origin=None, host=None):
        headers = {}
        if host is not None:
            headers["Host"] = host
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        if origin is not None:
            headers["Origin"] = origin
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data)
        status, response_headers, response = self.request(method, "/api/drawing", body=body, headers=headers)
        self.assertEqual(response_headers["Cache-Control"], "no-store")
        self.assertNotIn(self.api_key.encode(), response)
        return status, json.loads(response)

    def test_page_is_linked_and_contains_no_saved_key(self):
        status, _, nav = self.request("GET", "/panel-shell.js")
        self.assertEqual(status, 200)
        self.assertIn(b"/drawing.html", nav)
        status, headers, page = self.request("GET", "/drawing.html")
        self.assertEqual(status, 200)
        self.assertIn("form-action 'none'", headers["Content-Security-Policy"])
        self.assertIn(b'type="password"', page)
        self.assertIn(b'id="drawing-fallbacks"', page)
        self.assertIn("所有 QQ 群聊与私聊共用额度".encode(), page)
        self.assertNotIn(self.api_key.encode(), page)

    def test_settings_require_local_authorised_requests(self):
        self.assertEqual(self.settings(authenticated=False)[0], 401)
        self.assertEqual(self.settings(host="host.example.invalid")[0], 403)
        self.assertEqual(self.settings("POST", {}, origin=self.origin, authenticated=False)[0], 401)
        self.assertEqual(self.settings("POST", {})[0], 403)
        self.assertEqual(self.settings("POST", {}, origin="http://localhost:18872")[0], 403)

    def test_round_trip_retains_blank_key_and_never_returns_it(self):
        status, initial = self.settings()
        self.assertEqual(status, 200)
        self.assertFalse(initial["data"]["enabled"])
        self.assertFalse(initial["data"]["has_api_key"])
        self.assertEqual(initial["data"]["quota"]["window_remaining"], 2)
        self.assertEqual(initial["data"]["quota"]["daily_remaining"], 20)
        self.assertEqual(initial["data"]["model"], "gemini-3.1-flash-image")
        self.assertEqual(len(initial["data"]["fallback_models"]), 4)

        status, saved = self.settings("POST", {
            "endpoint": "https://example.invalid/v1beta",
            "model": "gemini-2.5-flash-image",
            "fallback_models": ["test-backup", "test-backup-2"],
            "api_key": self.api_key,
            "enabled": True,
            "reserve_usd": "0.05",
            "daily_usd_limit": "1.20",
            "monthly_usd_limit": "20",
        }, origin=self.origin)
        self.assertEqual(status, 200, saved)
        self.assertTrue(saved["data"]["enabled"])
        self.assertNotIn("api_key", saved["data"])
        self.assertEqual(self.settings()[1], saved)

        status, updated = self.settings("POST", {
            "enabled": False,
            "api_key": "  ",
            "daily_usd_limit": "",
        }, origin=self.origin)
        self.assertEqual(status, 200, updated)
        self.assertFalse(updated["data"]["enabled"])
        self.assertTrue(updated["data"]["has_api_key"])
        self.assertEqual(updated["data"]["fallback_models"], ["test-backup", "test-backup-2"])
        self.assertIsNone(updated["data"]["daily_usd_limit"])

        status, rejected = self.settings("POST", {
            "endpoint": "http://example.invalid/v1beta",
            "api_key": self.api_key,
        }, origin=self.origin)
        self.assertEqual(status, 400, rejected)
        self.assertEqual(self.settings()[1], updated)

    def test_quota_snapshot_appears_in_settings_reads_and_saves(self):
        quota = {"window_remaining": 0, "daily_remaining": 18, "window_reset_at": 400,
                 "day_reset_at": 800, "available_at": 400}
        with patch("hakimi.tools.drawing_accounting.quota_status", return_value=quota):
            status, settings = self.settings()
            self.assertEqual(status, 200)
            self.assertEqual(settings["data"]["quota"], quota)
            status, saved = self.settings("POST", {"enabled": False}, origin=self.origin)
            self.assertEqual(status, 200)
            self.assertEqual(saved["data"]["quota"], quota)

    def test_quota_lookup_error_does_not_prevent_saving_settings(self):
        with patch("hakimi.tools.drawing_accounting.quota_status", side_effect=sqlite3.OperationalError):
            status, saved = self.settings("POST", {"model": "synthetic-drawing-model"}, origin=self.origin)
            self.assertEqual(status, 200)
            self.assertIsNone(saved["data"]["quota"])
            self.assertEqual(self.settings()[1]["data"]["model"], "synthetic-drawing-model")


if __name__ == "__main__":
    unittest.main()
