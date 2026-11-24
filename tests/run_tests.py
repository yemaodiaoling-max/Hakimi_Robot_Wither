"""Run the suite; tests listed in known_stale run as expected failures.

    python tests/run_tests.py [-v] [test_module ...]
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "src"), str(HERE.parent)]
import os  # noqa: E402
os.environ.setdefault("HAKIMI_OWNER_FILE", str(HERE / "fixtures" / "owner.json"))
import hakimi  # noqa: E402,F401  (installs the historical module names)
from known_stale import STALE  # noqa: E402


def _iter(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter(item)
        else:
            yield item


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    names = [a for a in argv if not a.startswith("-")]
    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromNames(names) if names else loader.discover(str(HERE), top_level_dir=str(HERE))
    marked = set()
    for test in _iter(suite):
        key = test.id()
        if key in STALE and key not in marked:
            cls, name = type(test), key.rsplit(".", 1)[1]
            setattr(cls, name, unittest.expectedFailure(getattr(cls, name)))
            marked.add(key)
    missing = sorted(set(STALE) - marked) if not names else []
    if missing:
        print("known_stale entries without a test:", *missing, sep="\n  ")
    verbosity = 2 if "-v" in argv else 1
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() and not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
