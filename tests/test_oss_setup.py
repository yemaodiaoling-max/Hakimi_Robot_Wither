"""Open-source setup panel, module registry and local gateway (offline, temporary folders only)."""
import json
import os
import ssl
import tempfile
import threading
import time
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
        self.root, self.gateway, self.home, self.video = base / "root", base / "gw", base / "home", base / "video"
        self.env = mock.patch.dict(os.environ, {
            "HAKIMI_ROOT": str(self.root), "HAKIMI_GATEWAY_DIR": str(self.gateway),
            "HAKIMI_VIDEO_DIR": str(self.video), "HAKIMI_VAULT_KEY_FILE": str(self.home / "vault.key"),
            "HAKIMI_OWNER_FILE": ""})

    def __enter__(self):
        self.env.start()
        from hakimi.core import owner
        owner.load.cache_clear()
        from hakimi.setup import settings as st
        self.patches = [mock.patch.object(st, "CREDENTIALS", self.home / "credentials.fernet"),
                        mock.patch.object(st, "CONSOLE_TOKEN", self.home / "console.token")]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()


class ModulesTests(unittest.TestCase):
    def test_enable_disable_and_units(self):
        with _Env() as env:
            from hakimi import modules
            self.assertEqual(modules.enabled(), ["core"])
            modules.save(["qq", "pet", "bogus"])
            self.assertEqual(modules.enabled(), ["core", "qq", "pet"])
            units = modules.units()
            self.assertIn("hakimi-gateway.service", units)
            self.assertIn("hakimi-worker.service", units)
            self.assertNotIn("hakimi-bilibili-watcher.service", units)
            modules.save(["video"])
            self.assertFalse(modules.is_enabled("qq"))
            payload = modules.status_payload()
            self.assertEqual({m["key"] for m in payload["modules"]}, {"core", "qq", "video", "pet"})

    def test_owner_defaults_never_grant_admin_by_generic_name(self):
        with _Env() as env:
            from hakimi.core import owner
            env.root.mkdir(parents=True)
            self.assertEqual(owner.owner_names(), frozenset())
            (env.root / "owner.json").write_text(json.dumps({"name": "小明", "ids": ["123456789"]}), encoding="utf-8")
            owner.load.cache_clear()
            self.assertEqual(owner.owner_names(), frozenset({"小明"}))
            self.assertEqual(owner.owner_session(), "private:123456789")


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class SetupTests(unittest.TestCase):
    def test_normalise_requires_model_key_and_validates_ids(self):
        with _Env():
            from hakimi.setup import settings as st
            with self.assertRaises(st.SetupError) as ctx:
                st.normalise({"modules": ["core"]})
            self.assertEqual(ctx.exception.field, "model.api_key")
            with self.assertRaises(st.SetupError) as ctx:
                st.normalise({"model": {"api_key": "k" * 20}, "owner": {"ids": ["12ab"]}})
            self.assertEqual(ctx.exception.field, "owner.ids")
            with self.assertRaises(st.SetupError) as ctx:
                st.normalise({"modules": ["qq"], "model": {"api_key": "k" * 20}, "qq": {"app_id": "102000001"}})
            self.assertEqual(ctx.exception.field, "qq.test_group")

    def test_apply_writes_module_configs_and_hides_secrets(self):
        with _Env() as env:
            from hakimi.setup import settings as st
            from hakimi.gateway import server as gw
            submitted = {
                "modules": ["qq", "video"],
                "bot": {"name": "测试猫"},
                "owner": {"name": "小明", "ids": "123456789, 987654321", "aliases": []},
                "model": {"api_key": "upstream-secret-value", "main_model": "main-x", "light_model": "light-x",
                          "strong_model": "strong-x", "routing": "dual", "monthly_limit_usd": "12.5",
                          "prices": {"main-x": {"input_usd_per_million": "1", "output_usd_per_million": "2"}}},
                "qq": {"app_id": "102000001", "app_secret": "qq-secret-value", "test_group": "600000001"},
                "video": {"priority_room": "12345", "monthly_usd": "5", "daily_usd": "0.5"},
            }
            s, secrets = st.normalise(submitted)
            report = st.apply(s, secrets, run_prepare=False)
            failed = [r for r in report if not r["ok"]]
            self.assertEqual(failed, [])
            saved = (env.root / st.SETTINGS_NAME).read_text(encoding="utf-8")
            for secret in ("upstream-secret-value", "qq-secret-value"):
                self.assertNotIn(secret, saved)
            self.assertEqual(gw.upstream_key(), "upstream-secret-value")
            config = gw.load_config()
            self.assertEqual(config["models"]["gemini-4-argon"], "main-x")
            self.assertEqual(config["models"]["gemini-4-carbon"], "strong-x")
            self.assertEqual(config["models"]["gemini-3.8-flash"], "light-x")
            self.assertEqual(json.loads((env.root / "task_routing.json").read_text())["mode"], "dual")
            route = json.loads((env.root / "state/vertex_route.json").read_text())
            self.assertTrue(route["enabled"])
            self.assertEqual(Path(route["gateway_directory"]), env.gateway)
            official = json.loads((env.root / "official_test_config.json").read_text())
            self.assertEqual(official["app_id"], "102000001")
            self.assertEqual(st.credentials()["app_secret"], "qq-secret-value")
            owner = json.loads((env.root / "owner.json").read_text(encoding="utf-8"))
            self.assertEqual(owner["pet_session"], "private:123456789")
            watcher = json.loads((env.video / "deploy/watcher.json").read_text())
            self.assertEqual(watcher["priority_room"], "12345")
            self.assertFalse(watcher["send_enabled"])
            self.assertEqual(json.loads((env.root / "modules.json").read_text())["enabled"], ["core", "qq", "video"])
            state = st.secret_state()
            self.assertTrue(state["model.api_key"] and state["qq.app_secret"])
            # A second save without secrets keeps them.
            s2, secrets2 = st.normalise({"bot": {"name": "改名猫"}})
            self.assertEqual(secrets2, {})
            st.apply(s2, secrets2, run_prepare=False)
            self.assertEqual(gw.upstream_key(), "upstream-secret-value")
            self.assertEqual(st.credentials()["app_secret"], "qq-secret-value")

    def test_vertex_route_accepts_open_gateway(self):
        with _Env() as env:
            from hakimi.setup import settings as st
            from hakimi.models import vertex_route
            s, secrets = st.normalise({"model": {"api_key": "x" * 30}})
            st.apply(s, secrets, run_prepare=False)
            # PRODUCTION_DIRECTORY is read when vertex_route is first imported; trust the test folder explicitly.
            route = vertex_route.load(env.root / "official_test_runtime", allowed_directories=[env.gateway])
            self.assertTrue(route.enabled, route.error)
            self.assertIsNone(route.error)
            key, cert = route.credentials()
            self.assertTrue(key and Path(cert).is_file())


class PersonaTests(unittest.TestCase):
    def test_override_wins_and_reset_restores_default(self):
        with _Env() as env:
            from hakimi import paths, persona
            from hakimi.setup import persona_catalog as catalog
            default = catalog.read("gemini_persona.txt")
            self.assertFalse(default["custom"])
            self.assertTrue(default["text"])
            catalog.save("gemini_persona.txt", "你是测试猫。\n")
            self.assertEqual((env.root / "persona" / "gemini_persona.txt").read_text(encoding="utf-8"), "你是测试猫。\n")
            persona.text.cache_clear()
            self.assertEqual(persona.text("gemini_persona.txt"), "你是测试猫。\n")
            catalog.save("style_guide.md", "# 测试风格\n")
            self.assertEqual(paths.persona("style_guide.md", env.root / "elsewhere"), env.root / "persona" / "style_guide.md")
            catalog.reset("gemini_persona.txt")
            persona.text.cache_clear()
            self.assertEqual(persona.text("gemini_persona.txt"), default["text"])
            with self.assertRaises(ValueError):
                catalog.save("gemini_persona.txt", "   ")
            with self.assertRaises(KeyError):
                catalog.save("../config.json", "x")

    def test_every_catalog_entry_has_a_packaged_default(self):
        from hakimi.setup import persona_catalog as catalog
        for entry in catalog.ENTRIES:
            if not entry.file.startswith("video/"):
                self.assertTrue(catalog.default_path(entry.file).is_file(), entry.file)

    def test_video_persona_folder_switches_when_customised(self):
        with _Env() as env:
            from hakimi.setup import persona_catalog as catalog
            defaults = env.video / "personas"
            defaults.mkdir(parents=True)
            (defaults / "hakimi_core.md").write_text("核心", encoding="utf-8")
            (defaults / "bilibili_overlay.md").write_text("平台", encoding="utf-8")
            self.assertEqual(catalog.video_persona_dir(), defaults)
            catalog.save("video/bilibili_overlay.md", "新的平台规则")
            custom = env.root / "persona" / "video"
            self.assertEqual(catalog.video_persona_dir(), custom)
            self.assertEqual((custom / "hakimi_core.md").read_text(encoding="utf-8"), "核心")


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class GatewayTests(unittest.TestCase):
    def test_budget_payload_is_valid_for_clients(self):
        with _Env() as env:
            from hakimi.gateway import server as gw
            from hakimi.models import vertex_route
            gw.init()
            payload = gw.budget_payload(env.gateway, gw.load_config())
            checked = vertex_route.validate_budget_payload(payload)
            self.assertFalse(checked["blocked"])
            config = gw.load_config()
            config["monthly_limit_usd"] = "0.000001"
            config["prices"] = {"m": {"input_usd_per_million": "1000", "output_usd_per_million": "1000"}}
            gw.record(env.gateway, config, request_id="r1", model="gemini-4-argon", upstream_model="m",
                      usage={"promptTokenCount": 10, "candidatesTokenCount": 10}, status=200)
            blocked = vertex_route.validate_budget_payload(gw.budget_payload(env.gateway, config))
            self.assertTrue(blocked["blocked"])
            self.assertEqual(blocked["block_reason"], gw.MONTHLY_EXHAUSTED)

    def test_streaming_proxy_maps_model_and_records_usage(self):
        with _Env() as env:
            from hakimi.gateway import server as gw
            seen = {}

            class Upstream(BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def do_POST(self):
                    seen["path"] = self.path
                    seen["key"] = self.headers.get("x-goog-api-key")
                    self.rfile.read(int(self.headers["Content-Length"]))
                    body = (b'data: {"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}\n\n'
                            b'data: {"usageMetadata":{"promptTokenCount":7,"candidatesTokenCount":3}}\n\n')
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            threading.Thread(target=upstream.serve_forever, daemon=True).start()
            gw.init()
            config = gw.load_config()
            config["upstream"]["base_url"] = f"http://127.0.0.1:{upstream.server_address[1]}"
            config["models"]["gemini-4-argon"] = "real-model"
            gw.save_config(config)
            gw.save_upstream_key("upstream-key")
            server = gw.GatewayServer(("127.0.0.1", 0), env.gateway)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(env.gateway / gw.TLS_CERT_NAME), str(env.gateway / gw.TLS_KEY_NAME))
            server.socket = context.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                port = server.server_address[1]
                client = ssl.create_default_context(cafile=str(env.gateway / gw.CA_NAME))
                opener = build_opener(ProxyHandler({}), HTTPSHandler(context=client))
                key = (env.gateway / gw.KEY_NAME).read_text().strip()
                request = Request(f"https://127.0.0.1:{port}/v1beta/models/gemini-4-argon:streamGenerateContent?alt=sse",
                                  data=b"{}", headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
                with opener.open(request, timeout=10) as response:
                    text = response.read().decode()
                    self.assertEqual(response.headers["X-Gateway-Model"], "gemini-4-argon")
                self.assertIn('"hi"', text)
                self.assertEqual(seen["path"], "/v1beta/models/real-model:streamGenerateContent?alt=sse")
                self.assertEqual(seen["key"], "upstream-key")
                self.assertEqual(gw.month_tokens(env.gateway)["input_tokens"], 7)
                bad = Request(f"https://127.0.0.1:{port}/budget", headers={"Authorization": "Bearer wrong"})
                with self.assertRaises(Exception):
                    opener.open(bad, timeout=10)
            finally:
                server.shutdown()
                upstream.shutdown()


if __name__ == "__main__":
    unittest.main()
