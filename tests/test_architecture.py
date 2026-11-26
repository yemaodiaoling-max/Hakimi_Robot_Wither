"""Package layers: no import may point to a higher layer unless hakimi.architecture lists why."""
import importlib
import unittest

from hakimi import architecture, _legacy


class ArchitectureTests(unittest.TestCase):
    def test_every_module_has_a_layer(self):
        unlayered, _, _ = architecture.check()
        self.assertEqual(unlayered, [], "add the package to hakimi.architecture.LAYERS")

    def test_no_new_upward_imports(self):
        _, violations, _ = architecture.check()
        self.assertEqual(violations, [], "import from a lower layer instead, or move the shared code down")

    def test_exceptions_list_only_shrinks(self):
        _, _, stale = architecture.check()
        self.assertEqual(stale, [], "this upward import is gone: delete it from EXCEPTIONS")

    def test_package_code_uses_current_module_paths(self):
        files, edges = architecture.scan()
        self.assertFalse(set(_legacy.MODULE_ALIASES) & set(files), "an alias shadows a real module")
        old = {importer for importer, target, _line, _lazy in architecture.scan_raw()
               if target in _legacy.MODULE_ALIASES}
        self.assertEqual(sorted(old - set(architecture.PENDING_PATH_UPDATES)), [])
        self.assertEqual(sorted(set(architecture.PENDING_PATH_UPDATES) - old), [],
                         "this module uses current paths now: delete it from PENDING_PATH_UPDATES")

    def test_old_paths_resolve_to_the_same_module(self):
        for old, new in _legacy.MODULE_ALIASES.items():
            if old.endswith("web_fetch_worker"):
                continue
            with self.subTest(old=old):
                self.assertIs(importlib.import_module(old), importlib.import_module(new))


if __name__ == "__main__":
    unittest.main()
