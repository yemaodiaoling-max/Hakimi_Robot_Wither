"""Independent configuration and toggle registry for optional auxiliary APIs and agents.

Open-source installs work out-of-the-box with a single gateway API key and have
expensive or external-CLI auxiliaries disabled by default.  Production instances
can point each auxiliary slot at its own independent API endpoint, credential,
or external agent without altering core logic.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from hakimi import paths
from hakimi.core.runtime_io import atomic_replace, read_json_cached

FILE_NAME = "aux_config.json"

REVIEW_MODES = frozenset({"main_gateway", "custom_api", "companion_duty", "disabled"})
QQ_BACKUP_MODES = frozenset({"disabled", "custom_api", "companion_duty"})
CONTEXT_JUDGE_MODES = frozenset({"heuristic", "main_gateway", "custom_api", "dsh"})
SEARCH_JUDGE_MODES = frozenset({"local_rules", "main_gateway", "custom_api", "dsh"})
PAID_SEARCH_MODES = frozenset({"disabled", "deepseek"})
FALLBACK_BACKENDS = frozenset({"http_api", "dsh"})
OPS_BACKENDS = frozenset({"codex_cli", "main_gateway", "custom_api"})
PROTOCOLS = frozenset({"openai_compatible", "gemini", "main_gateway"})

DEFAULTS = {
    "version": 1,
    "review": {
        "enabled": True,
        "mode": "main_gateway",
        "model": "gemini-4-hydrogen",
        "protocol": "openai_compatible",
        "base_url": "",
        "api_key_file": "",
        "credential_key": "review",
        "output_limit_parameter": "max_tokens",
        "reasoning_effort": "low",
        "config_path": "",
    },
    "qq_backup": {
        "enabled": False,
        "mode": "disabled",
        "protocol": "openai_compatible",
        "base_url": "",
        "model": "",
        "fallback_model": "",
        "api_key_file": "",
        "credential_key": "qq_backup",
        "output_limit_parameter": "max_tokens",
        "reasoning_effort": "low",
        "config_path": "",
    },
    "context_judge": {
        "mode": "heuristic",
        "model": "gemini-4-hydrogen",
        "protocol": "openai_compatible",
        "base_url": "",
        "api_key_file": "",
        "credential_key": "context_judge",
        "output_limit_parameter": "max_tokens",
        "reasoning_effort": "low",
    },
    "search_routing": {
        "enabled": False,
        "judge_mode": "local_rules",
        "model": "gemini-4-hydrogen",
        "protocol": "openai_compatible",
        "base_url": "",
        "api_key_file": "",
        "credential_key": "search_judge",
        "paid_search_mode": "disabled",
        "dsh_on_budget": False,
    },
    "fallback": {
        "enabled": False,
        "backend": "http_api",
        "protocol": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key_file": "",
        "credential_key": "fallback",
    },
    "diary": {
        "fallback_enabled": False,
    },
    "ops": {
        "watchdog_enabled": False,
        "terra_supervision_enabled": False,
        "sol_repair_enabled": False,
        "codex_help_enabled": False,
        "backend": "codex_cli",
        "model": "",
        "protocol": "openai_compatible",
        "base_url": "",
        "api_key_file": "",
        "credential_key": "ops",
    },
}


def root_for(base=None) -> Path:
    path = Path(base) if base is not None else paths.root()
    path = path.resolve()
    return path.parent if path.name == paths.RUNTIME_NAME else path


def config_path(base=None) -> Path:
    return root_for(base) / FILE_NAME


def has_config(base=None) -> bool:
    return config_path(base).is_file()


def _merge(target: dict, override: dict | None) -> dict:
    if not isinstance(override, dict):
        return target
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
    return target


def load(base=None) -> dict:
    data = copy.deepcopy(DEFAULTS)
    path = config_path(base)
    if path.is_file():
        try:
            raw = read_json_cached(path, {})
        except (OSError, ValueError):
            raw = {}
        _merge(data, raw)
    return data


def save(value: dict, base=None) -> Path:
    clean = copy.deepcopy(DEFAULTS)
    _merge(clean, value)
    for section in clean.values():
        if isinstance(section, dict):
            section.pop("api_key", None)
    path = config_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write((json.dumps(clean, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    atomic_replace(tmp, path)
    return path


def review_config(base=None) -> dict:
    cfg = copy.deepcopy(load(base)["review"])
    if cfg.get("mode") not in REVIEW_MODES:
        cfg["mode"] = "main_gateway"
    return cfg


def qq_backup_config(base=None) -> dict:
    cfg = copy.deepcopy(load(base)["qq_backup"])
    if cfg.get("mode") not in QQ_BACKUP_MODES:
        cfg["mode"] = "disabled"
    return cfg


def context_judge_config(base=None) -> dict:
    cfg = copy.deepcopy(load(base)["context_judge"])
    if cfg.get("mode") not in CONTEXT_JUDGE_MODES:
        cfg["mode"] = "heuristic"
    return cfg


def search_routing_config(base=None) -> dict:
    cfg = copy.deepcopy(load(base)["search_routing"])
    if cfg.get("judge_mode") not in SEARCH_JUDGE_MODES:
        cfg["judge_mode"] = "local_rules"
    if cfg.get("paid_search_mode") not in PAID_SEARCH_MODES:
        cfg["paid_search_mode"] = "disabled"
    return cfg


def _legacy_config_fallback(base) -> dict:
    for candidate in (Path(base) / "config.json", root_for(base) / "config.json"):
        try:
            data = read_json_cached(candidate, {})
            if isinstance(data, dict) and isinstance(data.get("fallback"), dict):
                return data["fallback"]
        except (OSError, ValueError):
            pass
    return {}


def fallback_config(base=None, legacy_fallback=None) -> dict:
    resolved_base = Path(base) if base is not None else paths.base()
    legacy = legacy_fallback if isinstance(legacy_fallback, dict) else _legacy_config_fallback(resolved_base)
    legacy_enabled = bool(legacy.get("enabled", False))
    if not has_config(resolved_base):
        cfg = copy.deepcopy(DEFAULTS["fallback"])
        cfg["enabled"] = legacy_enabled
        cfg["backend"] = "dsh"
        return cfg
    cfg = copy.deepcopy(load(resolved_base)["fallback"])
    if cfg.get("backend") not in FALLBACK_BACKENDS:
        cfg["backend"] = "http_api"
    cfg["enabled"] = bool(cfg.get("enabled") and (legacy.get("enabled", True) if legacy else True))
    return cfg


def diary_fallback_enabled(base=None) -> bool:
    resolved_base = Path(base) if base is not None else paths.root()
    if has_config(resolved_base):
        return bool(load(resolved_base).get("diary", {}).get("fallback_enabled", False))
    return bool(_legacy_config_fallback(resolved_base).get("enabled", False))


def ops_config(base=None) -> dict:
    resolved_base = Path(base) if base is not None else paths.root()
    if not has_config(resolved_base):
        return {
            "watchdog_enabled": True,
            "terra_supervision_enabled": True,
            "sol_repair_enabled": True,
            "codex_help_enabled": True,
            "backend": "codex_cli",
            "model": "",
            "protocol": "openai_compatible",
            "base_url": "",
            "api_key_file": "",
            "credential_key": "ops",
        }
    cfg = copy.deepcopy(load(resolved_base)["ops"])
    if cfg.get("backend") not in OPS_BACKENDS:
        cfg["backend"] = "codex_cli"
    return cfg


def credentials_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("HAKIMI_CREDENTIALS_FILE", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".config" / "hakimi" / "credentials.fernet"


def _load_vault_credentials(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        from hakimi.core.host_secrets import crypt
        data = json.loads(crypt(path.read_bytes(), False))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve_api_key(section_cfg: dict, *, credentials_file: Path | None = None) -> str:
    if not isinstance(section_cfg, dict):
        return ""
    inline = str(section_cfg.get("api_key") or "").strip()
    if inline:
        return inline
    key_file = str(section_cfg.get("api_key_file") or "").strip()
    if key_file:
        p = Path(key_file)
        if p.is_absolute() and p.is_file():
            try:
                return p.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
    cred_key = str(section_cfg.get("credential_key") or "").strip()
    if cred_key:
        creds = _load_vault_credentials(credentials_path(credentials_file))
        aux_entry = (creds.get("aux") or {}).get(cred_key) or {}
        key = str(aux_entry.get("api_key") or "").strip()
        if key:
            return key
        if cred_key == "fallback":
            return str((creds.get("provider") or {}).get("api_key") or "").strip()
    return ""
