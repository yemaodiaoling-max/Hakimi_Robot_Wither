import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bili_companion.platform import auth


def rows():
    return [dict(name="SESSDATA", value="synthetic-session", domain=".bilibili.com",
                 path="/", secure=True, expires=4102444800),
            dict(name="DedeUserID", value="123", domain=".bilibili.com",
                 path="/", secure=True, expires=4102444800)]


class Response(io.BytesIO):
    def geturl(self):
        return auth.NAV_URL


class AuthTests(unittest.TestCase):
    def test_domain_filter_and_no_csrf(self):
        extra = [dict(rows()[0], domain="bilibili.com.evil.test"),
                 dict(rows()[0], domain="evil-bilibili.com"),
                 dict(rows()[0], name="bili_jct"),
                 dict(rows()[0], domain="b23.tv")]
        selected = auth.filter_cookies(rows() + extra)
        self.assertEqual(len(selected), 3)
        self.assertEqual({r["name"] for r in selected}, {"SESSDATA", "DedeUserID"})
        jar = auth.cookie_jar(selected)
        self.assertTrue(all(c.secure for c in jar))

    def test_expired_and_uid_conflict(self):
        with self.assertRaises(auth.AuthError) as exc:
            auth.filter_cookies([dict(r, expires=1) for r in rows()])
        self.assertEqual(exc.exception.code, "expired")
        with self.assertRaises(auth.AuthError) as exc:
            auth._expected_uid(rows() + [dict(rows()[1], value="456")])
        self.assertEqual(exc.exception.code, "mismatch")

    def test_fixed_network_get_and_login_binding(self):
        for body, expected in ((dict(code=0, data=dict(isLogin=True, mid=123)), None),
                               (dict(code=-101, data=dict(isLogin=False)), "expired"),
                               (dict(code=0, data=dict(isLogin=True, mid=456)), "mismatch")):
            with self.subTest(body=body), patch.object(auth.urllib.request, "build_opener") as build:
                build.return_value.open.return_value = Response(json.dumps(body).encode())
                if expected:
                    with self.assertRaises(auth.AuthError) as exc:
                        auth.check_nav(auth.cookie_jar(rows()), "123")
                    self.assertEqual(exc.exception.code, expected)
                else:
                    self.assertTrue(auth.check_nav(auth.cookie_jar(rows()), "123")["same_account"])
                request = build.return_value.open.call_args.args[0]
                self.assertEqual(request.full_url, auth.NAV_URL)
                self.assertEqual(request.get_method(), "GET")
                self.assertIsNone(request.data)

    def test_redirect_rejected(self):
        with self.assertRaises(auth.AuthError) as exc:
            auth._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test")
        self.assertEqual(exc.exception.code, "redirect")

    def test_cli_does_not_leak_errors(self):
        output = io.StringIO()
        with patch.object(auth.os, "name", "posix"), patch.object(auth, "status", side_effect=RuntimeError("secret-value")), contextlib.redirect_stdout(output):
            self.assertEqual(auth.main(["status", "--state-dir", "unused"]), 1)
        self.assertNotIn("secret-value", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["state"], "local_error")


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "optional auth dependency unavailable")
class EncryptionTests(unittest.TestCase):
    def test_independent_roundtrip_and_sources_unchanged(self):
        from cryptography.fernet import Fernet
        state = Path(__file__).resolve().parents[1] / "state"
        state.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=state) as temporary:
            root = Path(temporary)
            key = Fernet.generate_key()
            source_key, source_vault = root / "source.key", root / "source.fernet"
            ciphertext = Fernet(key).encrypt(json.dumps(rows()).encode())
            source_key.write_bytes(key)
            source_vault.write_bytes(ciphertext)
            target = root / "new"
            with patch.object(auth, "check_nav", return_value=dict(logged_in=True, same_account=True)) as check:
                self.assertTrue(auth.import_qq(source_vault, source_key, target)["same_account"])
                self.assertEqual(check.call_count, 2)
            self.assertNotEqual((target / "vault.key").read_bytes(), key)
            self.assertEqual(source_key.read_bytes(), key)
            self.assertEqual(source_vault.read_bytes(), ciphertext)
            jar, expected = auth.load_cookie_jar(target)
            self.assertEqual(expected, "123")
            self.assertEqual(len(list(jar)), 2)
            if auth.os.name == "posix":
                self.assertEqual(target.stat().st_mode & 0o777, 0o700)
                for name in ("vault.key", "cookies.fernet"):
                    self.assertEqual((target / name).stat().st_mode & 0o777, 0o600)
            # Independent status must not touch the old vault/key.
            source_key.unlink()
            source_vault.unlink()
            with patch.object(auth, "check_nav", return_value=dict(logged_in=True, same_account=True)):
                self.assertTrue(auth.status(target)["logged_in"])

    def test_mismatch_does_not_persist_target(self):
        from cryptography.fernet import Fernet
        state = Path(__file__).resolve().parents[1] / "state"
        state.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=state) as temporary:
            root = Path(temporary)
            key = Fernet.generate_key()
            source_key, source_vault = root / "source.key", root / "source.fernet"
            source_key.write_bytes(key)
            source_vault.write_bytes(Fernet(key).encrypt(json.dumps(rows()).encode()))
            target = root / "new"
            with patch.object(auth, "check_nav", side_effect=auth.AuthError("mismatch")):
                with self.assertRaises(auth.AuthError):
                    auth.import_qq(source_vault, source_key, target)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
