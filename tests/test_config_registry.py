import json, shutil, tempfile, unittest
from pathlib import Path

from hakimi import config, paths


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class ConfigRegistryTests(unittest.TestCase):
    def instance(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        runtime = root / paths.RUNTIME_NAME
        for name in ("config.json", "group_profiles.json", "social_behavior_policy.json", "codex_help_config.json",
                     "official_test_config.json", "file_send_config.json", "video_tools_config.json"):
            data = json.loads(paths.default_config(name).read_text(encoding="utf-8"))
            write(root / name, data)
            if name in ("config.json", "group_profiles.json", "social_behavior_policy.json"):
                if name == "config.json":
                    data = dict(data, official_lane=True)
                write(runtime / name, data)
        write(runtime / "official_lane_config.json", json.loads((root / "official_test_config.json").read_text()))
        return root

    def errors(self, root):
        return [f for f in config.check(root) if f[0] == "error"]

    def test_packaged_defaults_form_a_valid_instance(self):
        self.assertEqual(self.errors(self.instance()), [])

    def test_broken_json_missing_keys_and_bad_references_are_reported(self):
        root = self.instance()
        (root / "codex_help_config.json").write_text("{not json", encoding="utf-8")
        profiles = json.loads((root / "group_profiles.json").read_text())
        profiles["groups"]["123456"] = {"profile": "no_such_style"}
        write(root / "group_profiles.json", profiles)
        cfg = json.loads((root / "config.json").read_text())
        del cfg["fallback"]
        cfg["bot_profile"]["active_groups"] = ["abc"]
        write(root / "config.json", cfg)
        messages = "\n".join(f"{label}: {msg}" for _, label, msg in self.errors(root))
        self.assertIn("root/codex_help_config.json: 不是有效 JSON", messages)
        self.assertIn("no_such_style", messages)
        self.assertIn("缺少字段 fallback", messages)
        self.assertIn("active_groups", messages)

    def test_load_prefers_instance_then_packaged_default(self):
        root = self.instance()
        write(root / "file_send_config.json", {"max_mb": {}, "upload_timeout_seconds": 7})
        self.assertEqual(config.load("file_send_config.json", root)["upload_timeout_seconds"], 7)
        empty = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.assertEqual(config.load("file_send_config.json", empty),
                         json.loads(paths.default_config("file_send_config.json").read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
