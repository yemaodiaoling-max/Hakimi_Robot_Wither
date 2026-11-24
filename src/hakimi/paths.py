"""Where code, packaged resources and instance data live.

Code is the installed ``hakimi`` package.  Instance data lives outside it:

* ``root()``    - the deployment folder (operator files, top-level ``state/``).
* ``runtime()`` - the bot lane data folder, ``<root>/official_test_runtime``.
* ``base()``    - the data folder of *this process*: ``runtime()`` for the bot
  lane (agent, worker, console, embedding), ``root()`` for operator services
  (supervisor, help, diary).  Services set it through ``HAKIMI_BASE``.

Before the package layout these were implied by where a copy of each module
sat on disk (``Path(__file__).parent``).  The environment variables
``HAKIMI_ROOT``, ``HAKIMI_RUNTIME`` and ``HAKIMI_BASE`` now state it.  Without
them a source checkout acts as its own root, as the flat folder used to.
"""
import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PERSONA_DIR = PACKAGE_DIR / "persona"
DEFAULTS_DIR = PACKAGE_DIR / "defaults"
RUNTIME_NAME = "official_test_runtime"


def _env_path(name):
    value = os.environ.get(name, "").strip()
    return Path(value).resolve() if value else None


def checkout_root():
    """The source checkout containing ``src/hakimi``, or None when installed."""
    if PACKAGE_DIR.parent.name == "src" and (PACKAGE_DIR.parent.parent / "pyproject.toml").is_file():
        return PACKAGE_DIR.parent.parent
    return None


def root():
    return _env_path("HAKIMI_ROOT") or (checkout_root() or Path.cwd()).resolve()


def runtime():
    return _env_path("HAKIMI_RUNTIME") or root() / RUNTIME_NAME


def base():
    return _env_path("HAKIMI_BASE") or root()


def persona_overrides():
    """Custom persona files saved by the setup panel (``<root>/persona``)."""
    return _env_path("HAKIMI_PERSONA_DIR") or root() / "persona"


def persona(name, base_dir=None):
    """A persona/style file: a custom copy in ``<root>/persona`` wins, then an instance
    copy in ``base_dir``, else the packaged one."""
    custom = persona_overrides() / name
    if custom.is_file():
        return custom
    if base_dir is not None:
        candidate = Path(base_dir) / name
        if candidate.is_file():
            return candidate
    return PERSONA_DIR / name


def default_config(name):
    return DEFAULTS_DIR / name


def source_file(name):
    """Source path of a module by its historical flat file name (``bridge_daemon.py``)."""
    from hakimi._legacy import LEGACY
    stem = name[:-3] if name.endswith(".py") else name
    dotted = LEGACY.get(stem)
    if dotted is None:
        return None
    return PACKAGE_DIR.joinpath(*dotted.split(".")[1:]).with_suffix(".py")


def install_dir():
    """Where the venv and the node tools (dsh, codex) are installed."""
    return _env_path("HAKIMI_OPT") or Path("/opt/hakimi")


def venv_bin():
    return install_dir() / "venv" / "bin"


def dsh_bin():
    return install_dir() / "dsh" / "node_modules" / ".bin"


def codex_executable():
    return install_dir() / "codex" / "node_modules" / ".bin" / "codex"


def gateway_dir():
    """Local model gateway directory (key, certificate, ledger); see ``hakimi gateway``."""
    return _env_path("HAKIMI_GATEWAY_DIR") or Path.home() / ".local" / "share" / "hakimi-gateway"
