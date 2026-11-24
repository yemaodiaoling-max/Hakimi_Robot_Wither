"""Optional modules sharing one memory core.

    hakimi modules                 # what is installed / enabled
    hakimi modules enable qq video
    hakimi modules disable pet
    hakimi modules units           # systemd units the enabled modules need (used by deploy/install.sh)

``core`` (long-term memory, relationship graph, console/setup panel, model gateway) is always on.
Every other module can be installed and enabled on its own; a module that is not enabled
is never started and the others do not depend on it at runtime:

* ``qq``    - QQ official bot (``hakimi`` package itself, services hakimi-worker / hakimi-agent ...)
* ``video`` - Bilibili watching companion (``bili_companion`` package, ``pip install ./bili_companion``)
* ``pet``   - desktop pet (Windows client in ``desktop_pet/``; its server side runs on demand)

The enabled set is stored in ``<root>/modules.json``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from hakimi import paths

FILE_NAME = "modules.json"


@dataclass(frozen=True)
class Module:
    key: str
    name: str
    summary: str
    units: tuple = ()
    optional_units: tuple = ()   # (unit, python module that must be importable)
    panel_pages: tuple = ()


MODULES = (
    Module("core", "记忆内核", "长期记忆、关系网、信息面板与设置面板、本地模型网关（始终启用）",
           ("hakimi-gateway.service", "hakimi-memory-console.service"),
           (("hakimi-memory-embedding.service", "fastembed"),),
           ("/", "/review-history.html", "/setup.html")),
    Module("qq", "QQ 机器人", "QQ 官方机器人通道：群聊/私聊回复、表情包、数学排版、日记",
           ("hakimi-worker.service", "hakimi-agent.service", "hakimi-diary.timer"),
           (("hakimi-help.service", "ops:codex_help"), ("hakimi-terra-watch.timer", "ops:supervisor")),
           ("/messages.html", "/identity.html")),
    Module("video", "视频机器人", "B 站看视频/直播、弹幕与评论，观看经历写入共享记忆",
           ("hakimi-bilibili-watcher.service", "hakimi-memory-bridge.timer"), (),
           ("/activity.html",)),
    Module("pet", "桌宠", "Windows 桌面宠物：看屏幕、语音对话，与 QQ/视频共用记忆", (), (), ()),
)
BY_KEY = {m.key: m for m in MODULES}
DEFAULT_ENABLED = ("core",)


def _root(root=None):
    return Path(root) if root else paths.root()


def installed(key: str) -> bool:
    if key in ("core", "qq"):
        return importlib.util.find_spec("hakimi") is not None
    if key == "video":
        if importlib.util.find_spec("bili_companion") is not None:
            return True
        return (video_dir() / "src" / "bili_companion").is_dir()
    if key == "pet":
        return importlib.util.find_spec("desktop_pet") is not None or (pet_dir() / "server_agent.py").is_file()
    return False


def video_dir() -> Path:
    """Checkout of the video module (``bili_companion``)."""
    env = os.environ.get("HAKIMI_VIDEO_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    checkout = paths.checkout_root()
    if checkout and (checkout / "bili_companion").is_dir():
        return checkout / "bili_companion"
    return paths.install_dir() / "bili_companion"


def pet_dir() -> Path:
    checkout = paths.checkout_root()
    if checkout and (checkout / "desktop_pet").is_dir():
        return checkout / "desktop_pet"
    return paths.install_dir() / "app" / "desktop_pet"


def enabled(root=None) -> list:
    try:
        value = json.loads((_root(root) / FILE_NAME).read_text(encoding="utf-8-sig"))
        keys = [k for k in value.get("enabled", []) if k in BY_KEY]
    except (OSError, ValueError, AttributeError):
        keys = list(DEFAULT_ENABLED)
    return ["core"] + [k for k in keys if k != "core"]


def is_enabled(key: str, root=None) -> bool:
    return key in enabled(root)


def save(keys, root=None) -> list:
    keys = ["core"] + sorted({k for k in keys if k in BY_KEY and k != "core"}, key=list(BY_KEY).index)
    path = _root(root) / FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "enabled": keys}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return keys


def units(root=None) -> list:
    result = []
    for key in enabled(root):
        module = BY_KEY[key]
        if not installed(key):
            continue
        result += list(module.units)
        for unit, requirement in module.optional_units:
            if requirement.startswith("ops:"):
                import shutil
                from hakimi.models import aux_config
                if not aux_config.has_config(root):
                    if shutil.which("codex") or paths.codex_executable().is_file():
                        result.append(unit)
                    continue
                ops = aux_config.ops_config(root)
                backend = ops.get("backend", "codex_cli")
                has_codex = bool(shutil.which("codex") or paths.codex_executable().is_file())
                if requirement == "ops:codex_help":
                    if ops.get("codex_help_enabled") and (backend != "codex_cli" or has_codex):
                        result.append(unit)
                elif requirement == "ops:supervisor":
                    ai_ops = bool(ops.get("terra_supervision_enabled") or ops.get("sol_repair_enabled"))
                    if ops.get("watchdog_enabled") or (ai_ops and (backend != "codex_cli" or has_codex)):
                        result.append(unit)
                continue
            if requirement.startswith("bin:"):
                import shutil
                if shutil.which(requirement[4:]):
                    result.append(unit)
                continue
            try:
                if importlib.util.find_spec(requirement) is not None:
                    result.append(unit)
            except (ImportError, ValueError):
                pass
    return result


def status_payload(root=None) -> dict:
    on = enabled(root)
    return {"modules": [{"key": m.key, "name": m.name, "summary": m.summary, "installed": installed(m.key),
                         "enabled": m.key in on, "pages": list(m.panel_pages)} for m in MODULES]}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hakimi modules", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", nargs="?", default="status", choices=["status", "enable", "disable", "units", "json"])
    parser.add_argument("names", nargs="*")
    parser.add_argument("--root")
    args = parser.parse_args(argv)
    bad = [n for n in args.names if n not in BY_KEY]
    if bad:
        parser.error("unknown module: " + ", ".join(bad))
    if args.action == "enable":
        print("enabled:", " ".join(save(enabled(args.root) + args.names, args.root)))
    elif args.action == "disable":
        if "core" in args.names:
            parser.error("core cannot be disabled")
        print("enabled:", " ".join(save([k for k in enabled(args.root) if k not in args.names], args.root)))
    elif args.action == "units":
        print("\n".join(units(args.root)))
    elif args.action == "json":
        print(json.dumps(status_payload(args.root), ensure_ascii=False))
    else:
        for row in status_payload(args.root)["modules"]:
            flag = "启用" if row["enabled"] else "未启用"
            inst = "已安装" if row["installed"] else "未安装"
            print(f"{row['key']:6s} {row['name']:8s} {inst} {flag}  {row['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
