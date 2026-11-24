"""Locate repository files for tests after the package layout."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "hakimi"
STATIC = PACKAGE / "console" / "memory_console_static"
FIXTURES = REPO / "tests" / "fixtures"
INSTANCE = FIXTURES / "instance"  # a real instance configuration (not shipped in the package)


def repo_file(name):
    """Historical top-level file name -> its current location."""
    if name.startswith("memory_console_static"):
        return PACKAGE / "console" / name
    for folder in (PACKAGE / "persona", INSTANCE, FIXTURES, PACKAGE / "defaults"):
        if (folder / name).exists():
            return folder / name
    if name.endswith((".py", ".mjs")):
        hits = list(PACKAGE.rglob(name))
        if len(hits) == 1:
            return hits[0]
    return REPO / name
