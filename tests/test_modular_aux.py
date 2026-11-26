"""Tests for single-API OSS defaults, OpenAI gateway support, and independent auxiliary configs."""
import json
import os
import shutil
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False


class _Env:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "root"
        self.runtime = self.root / "official_test_runtime"
        self.gateway = base / "gw"
        self.home = base / "home"
        self.video = base / "video"
        self.env = mock.patch.dict(
            os.environ,
            {
                "HAKIMI_ROOT": str(self.root),
                "HAKIMI_BASE": str(self.runtime),
                "HAKIMI_GATEWAY_DIR": str(self.gateway),
                "HAKIMI_VIDEO_DIR": str(self.video),
                "HAKIMI_VAULT_KEY_FILE": str(self.home / "vault.key"),
                "HAKIMI_CREDENTIALS_FILE": str(self.home / "credentials.fernet"),
                "HAKIMI_OWNER_FILE": "",
            },
        )

    def __enter__(self):
        self.env.start()
        from hakimi.core import owner
        owner.load.cache_clear()
        from hakimi.setup import settings as st
        self.patches = [
            mock.patch.object(st, "CREDENTIALS", self.home / "credentials.fernet"),
            mock.patch.object(st, "CONSOLE_TOKEN", self.home / "console.token"),
        ]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class OpenAIGatewayTests(unittest.TestCase):
    def test_openai_compatible_gateway_non_stream_sse_tools_and_models(self):
        with _Env() as env:
            from hakimi.gateway import server as gw

            requests_seen = []

            class Upstream(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"data": [{"id": "deepseek-chat"}, {"id": "gpt-4o-mini"}]}).encode())

                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    requests_seen.append((self.path, self.headers.get("Authorization"), body))
                    if body.get("stream"):
                        tool_name = body["tools"][0]["function"]["name"]
                        chunks = [
                            {"model": "deepseek-chat", "choices": [{"index": 0, "delta": {"content": "hello "}}]},
                            {
                                "model": "deepseek-chat",
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "tool_calls": [
                                                {"index": 0, "id": "c1", "function": {"name": tool_name, "arguments": "{\"q\":\"cat\"}"}}
                                            ]
                                        },
                                        "finish_reason": "tool_calls",
                                    }
                                ],
                                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                            },
                        ]
                        payload = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
                        raw = payload.encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                    else:
                        resp = {
                            "model": "deepseek-chat",
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": "{\"ok\":true}"}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                        }
                        raw = json.dumps(resp).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)

            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            threading.Thread(target=upstream.serve_forever, daemon=True).start()
            gw.init()
            cfg = gw.load_config()
            cfg["upstream"]["protocol"] = "openai_compatible"
            cfg["upstream"]["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
            cfg["models"]["gemini-4-argon"] = "deepseek-chat"
            cfg["models"]["gemini-4-hydrogen"] = "deepseek-chat"
            gw.save_config(cfg)
            gw.save_upstream_key("sk-test-openai-key")
            server = gw.GatewayServer(("127.0.0.1", 0), env.gateway)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(env.gateway / gw.TLS_CERT_NAME), str(env.gateway / gw.TLS_KEY_NAME))
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]
                ca = ssl.create_default_context(cafile=str(env.gateway / gw.CA_NAME))
                opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ca))
                key = (env.gateway / gw.KEY_NAME).read_text().strip()

                self.assertEqual(gw.list_models(cfg, "sk-test-openai-key"), ["deepseek-chat", "gpt-4o-mini"])

                req1 = Request(
                    f"https://127.0.0.1:{port}/v1beta/models/gemini-4-hydrogen:generateContent",
                    data=json.dumps(
                        {
                            "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
                            "generationConfig": {"responseMimeType": "application/json"},
                        }
                    ).encode(),
                    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                )
                with opener.open(req1, timeout=10) as resp1:
                    g1 = json.loads(resp1.read())
                self.assertEqual(g1["candidates"][0]["content"]["parts"][0]["text"], "{\"ok\":true}")
                self.assertEqual(g1["usageMetadata"]["promptTokenCount"], 5)

                req2 = Request(
                    f"https://127.0.0.1:{port}/v1beta/models/gemini-4-argon:streamGenerateContent?alt=sse",
                    data=json.dumps(
                        {
                            "contents": [{"role": "user", "parts": [{"text": "search cat"}]}],
                            "tools": [{"functionDeclarations": [{"name": "memory.search", "description": "s", "parametersJsonSchema": {"type": "object"}}]}],
                        }
                    ).encode(),
                    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                )
                with opener.open(req2, timeout=10) as resp2:
                    sse_lines = [l for l in resp2.read().decode().splitlines() if l.startswith("data:")]
                events = [json.loads(l[5:]) for l in sse_lines]
                parts = [p for ev in events for p in ev["candidates"][0]["content"]["parts"]]
                self.assertEqual(parts[0]["text"], "hello ")
                self.assertEqual(parts[1]["functionCall"]["name"], "memory.search")
                self.assertEqual(parts[1]["functionCall"]["args"], {"q": "cat"})
                self.assertEqual(gw.month_tokens(env.gateway)["input_tokens"], 16)
                self.assertEqual(requests_seen[0][0], "/v1/chat/completions")
                self.assertEqual(requests_seen[0][1], "Bearer sk-test-openai-key")
            finally:
                server.shutdown()
                upstream.shutdown()


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class SingleApiDefaultsTests(unittest.TestCase):
    def test_oss_single_api_defaults_require_no_cli_or_companion(self):
        with _Env() as env:
            from hakimi import modules
            from hakimi.bridge import conversation_judge as judge
            from hakimi.features import diary_daily
            from hakimi.models import aux_config, search_routing, vertex_route, video_hydrogen, web_access
            from hakimi.curation import review_runtime
            from hakimi.setup import settings as st

            s, secrets = st.normalise(
                {
                    "modules": ["qq"],
                    "model": {
                        "provider": "openai_compatible",
                        "base_url": "https://api.example.invalid/v1",
                        "api_key": "single-api-secret",
                        "main_model": "gpt-4o-mini",
                    },
                    "qq": {"app_id": "102000001", "app_secret": "qq-secret", "test_group": "600000001"},
                }
            )
            report = st.apply(s, secrets, run_prepare=False)
            self.assertEqual([r for r in report if not r["ok"]], [])

            aux = aux_config.load(env.root)
            self.assertFalse(aux["qq_backup"]["enabled"])
            self.assertEqual(aux["qq_backup"]["mode"], "disabled")
            self.assertFalse(aux["fallback"]["enabled"])
            self.assertFalse(aux["search_routing"]["enabled"])
            self.assertEqual(aux["context_judge"]["mode"], "heuristic")
            self.assertEqual(aux["review"]["mode"], "main_gateway")
            self.assertFalse(aux["diary"]["fallback_enabled"])
            self.assertFalse(aux["ops"]["watchdog_enabled"])
            self.assertFalse(aux["ops"]["terra_supervision_enabled"])
            self.assertFalse(aux["ops"]["sol_repair_enabled"])
            self.assertFalse(aux["ops"]["codex_help_enabled"])

            # Even if `codex` exists on host PATH, ops units stay off unless enabled in aux_config.
            with mock.patch.object(shutil, "which", return_value="/usr/bin/codex"):
                units = modules.units(env.root)
            self.assertIn("hakimi-worker.service", units)
            self.assertNotIn("hakimi-help.service", units)
            self.assertNotIn("hakimi-terra-watch.timer", units)

            # Backup channel is off and never touches bili_companion.
            self.assertIsNone(video_hydrogen.config(env.root))

            # Context judge runs purely locally without dsh_text_judge.
            rows = [
                {"message_id": "1", "user_id": "100", "session_key": "group:1", "received_at": 10, "text": "早安"},
                {"message_id": "2", "user_id": "100", "session_key": "group:1", "received_at": 11, "text": "@哈基米 ping"},
            ]
            with mock.patch.object(judge, "run_judge", side_effect=AssertionError("must not call dsh")):
                picked = judge.mention_context({}, rows, [rows[1]], env.runtime)
                self.assertEqual([r["message_id"] for r in picked], ["1", "2"])
                cont = judge.continuation_context({}, [{"message_id": "3", "user_id": "100", "text": "你好", "reply_to": "9"}], "", env.runtime, own_message_ids=["9"])
                self.assertEqual(cont, ["3"])

            # Presearch is off by default; explicit web.search skips DeepSeek paid endpoint.
            payload = {"session_key": "group:1", "trigger_id": "t1", "reply_target_message_id": "1", "recent_context_messages": [{"message_id": "1", "user_id": "u", "text": "帮我搜一下今天新闻"}]}
            self.assertIsNone(search_routing.prepare(payload, env.runtime))
            transport = mock.Mock(return_value={"body": "<rss><channel></channel></rss>", "billable": False})
            web_access.run("web.search", {"query": "cat news"}, 2, env.runtime, payload, transport=transport)
            self.assertNotIn("search", [c.args[0] for c in transport.call_args_list])

            # Diary never calls dsh when diary.fallback_enabled is False.
            with mock.patch.object(diary_daily, "ROOT", env.root), \
                    mock.patch("hakimi.models.vertex_route.load", return_value=mock.Mock(enabled=False)), \
                    mock.patch("hakimi.models.agy_session.AgySession.generate", side_effect=RuntimeError("no agy")):
                with self.assertRaisesRegex(RuntimeError, "diary_primary_unavailable"):
                    diary_daily.generate("2026-11-24")

            # Review via main_gateway uses local gateway without bili_companion.
            fake_gw_resp = {
                "modelVersion": "gpt-4o-mini",
                "candidates": [{"content": {"role": "model", "parts": [{"text": "{\"relationship_events\":[]}"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 4},
            }
            with mock.patch.object(vertex_route, "PRODUCTION_DIRECTORY", env.gateway), \
                    mock.patch("hakimi.models.gateway_text.build_opener") as opener:
                resp_obj = mock.MagicMock()
                resp_obj.__enter__.return_value = resp_obj
                resp_obj.read.return_value = json.dumps(fake_gw_resp).encode()
                opener.return_value.open.return_value = resp_obj
                res = review_runtime.classify("return json", env.runtime)
                self.assertEqual(res, {"relationship_events": []})
                self.assertEqual(review_runtime.reviewer_metadata(res)["reviewer"], "local_gateway")


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class IndependentAuxModesTests(unittest.TestCase):
    def test_custom_review_backup_fallback_and_ops_flags(self):
        with _Env() as env:
            from hakimi.curation import post_reply_review, review_runtime
            from hakimi.models import aux_config, fallback_http, video_hydrogen
            from hakimi.ops import cloud_supervisor

            class MockAPI(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    if body.get("stream"):
                        chunks = [
                            {"model": "backup-m", "choices": [{"index": 0, "delta": {"content": "backup hi"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
                        ]
                        raw = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                    if body.get("model") == "rev-m":
                        text = json.dumps({"relationship_events": []})
                    else:
                        text = json.dumps({"action": "reply", "text": "fallback ok"})
                    raw = json.dumps({"model": body["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 4}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

            srv = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
                key_file = env.home / "aux.key"
                key_file.parent.mkdir(parents=True, exist_ok=True)
                key_file.write_text("aux-secret-key", encoding="utf-8")

                cfg = aux_config.load(env.root)
                cfg["review"].update(enabled=True, mode="custom_api", protocol="openai_compatible", base_url=url, model="rev-m", api_key_file=str(key_file))
                cfg["qq_backup"].update(enabled=True, mode="custom_api", protocol="openai_compatible", base_url=url, model="backup-m", api_key_file=str(key_file))
                cfg["fallback"].update(enabled=True, backend="http_api", protocol="openai_compatible", base_url=url, model="fb-m", api_key_file=str(key_file))
                cfg["ops"].update(watchdog_enabled=True, terra_supervision_enabled=False, sol_repair_enabled=False, codex_help_enabled=False)
                aux_config.save(cfg, env.root)

                # 1. Custom review API
                rev = review_runtime.classify("return json", env.runtime)
                self.assertEqual(review_runtime.reviewer_metadata(rev)["reviewer"], "custom_review_api")

                # 2. Custom QQ backup API
                bcfg = video_hydrogen.config(env.root)
                self.assertEqual(bcfg["mode"], "custom_api")
                self.assertEqual(video_hydrogen.duty_identity(bcfg), ("qq_backup_api", "backup-m"))
                sess = mock.Mock(_owns_turn=lambda: True, _progress=None, last_error=None)
                bres = video_hydrogen.request(sess, {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}, 10, bcfg, env.root)
                self.assertEqual(bres["_provider"], "qq_backup_api")
                self.assertEqual(bres["candidates"][0]["content"]["parts"][0]["text"], "backup hi")

                # 3. HTTP fallback without DSH
                fres = fallback_http.run_http_fallback("ping", env.runtime, timeout_seconds=10)
                self.assertEqual(fres, {"action": "reply", "text": "fallback ok"})

                # 4. Disabled review stops post_reply_review cleanly
                cfg["review"]["enabled"] = False
                aux_config.save(cfg, env.root)
                self.assertEqual(post_reply_review.tick(env.runtime)["reason"], "review_disabled")

                # 5. Companion duty honors independent config_path instead of watcher.json
                indep = env.root / "state" / "indep_review.json"
                indep.parent.mkdir(parents=True, exist_ok=True)
                indep.write_text('{"monthly_usd":"1"}', encoding="utf-8")
                fake_mod = mock.Mock()
                fake_mod.load_config.return_value = {"monthly_usd": "1"}
                with mock.patch.dict("sys.modules", {"bili_companion": mock.Mock(), "bili_companion.cognition": mock.Mock(companion_text=fake_mod)}):
                    _, loaded = video_hydrogen.review_backend(env.root, config_path=str(indep))
                fake_mod.load_config.assert_called_once_with(indep)
                self.assertEqual(loaded, {"monthly_usd": "1"})

                # 6. Watchdog runs without Terra/Sol when those are False
                state_dir = env.root / "state" / "terra_watch"
                state_dir.mkdir(parents=True, exist_ok=True)
                with mock.patch.multiple(cloud_supervisor, ROOT=env.root, STATE=state_dir, RUNTIME=env.runtime / "state"), \
                        mock.patch.object(cloud_supervisor, "quick_faults", return_value=[]), \
                        mock.patch.object(cloud_supervisor, "inspect", return_value=["delivery_paused"]), \
                        mock.patch.object(cloud_supervisor, "recover_services", return_value=({}, False)), \
                        mock.patch("sys.argv", ["supervisor"]), \
                        mock.patch("hakimi.ops.terra_supervision.run_cycle", side_effect=AssertionError("terra off")), \
                        mock.patch("hakimi.ops.sol_repair.run_cycle", side_effect=AssertionError("sol off")):
                    cloud_supervisor.main()
                latest = json.loads((state_dir / "latest.json").read_text(encoding="utf-8"))
                self.assertEqual(latest["status"], "needs_attention")
                scheduled = json.loads((state_dir / "scheduled_results.json").read_text(encoding="utf-8"))
                self.assertEqual(scheduled["terra"]["status"], "disabled")
            finally:
                srv.shutdown()


if __name__ == "__main__":
    unittest.main()
