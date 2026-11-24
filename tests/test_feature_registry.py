import importlib.util
import unittest

from hakimi.features.registry import FEATURES, markdown
from hakimi.models.dsh_tools import DSH_ONLY
from hakimi.prompt.tool_catalog import TOOLS


class FeatureRegistryTests(unittest.TestCase):
    def test_every_callable_tool_belongs_to_a_feature(self):
        tools = {t["name"] for t in TOOLS} | set(DSH_ONLY)
        registered = [t for f in FEATURES for t in f.tools]
        self.assertEqual(sorted(tools - set(registered)), [], "register new tools in hakimi/features/registry.py")
        self.assertEqual(sorted(set(registered) - tools), [])
        self.assertEqual(len(registered), len(set(registered)), "a tool is listed under two features")

    def test_modules_exist_and_keys_are_unique(self):
        for feature in FEATURES:
            for module in feature.modules:
                self.assertIsNotNone(importlib.util.find_spec("hakimi." + module), module)
        self.assertEqual(len({f.key for f in FEATURES}), len(FEATURES))
        self.assertIn("## 关系网", markdown())
