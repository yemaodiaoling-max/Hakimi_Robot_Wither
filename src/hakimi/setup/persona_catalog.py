"""Persona entries the setup panel can edit.

Every entry has a packaged default; saving writes ``<root>/persona/<file>`` and "恢复默认"
deletes that copy.  Video-module entries live under ``<root>/persona/video/`` and are used by
``bili_companion`` through ``watcher.json: persona_dir`` (set by ``hakimi setup``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from hakimi import modules, paths

MAX_CHARS = 9000   # the console accepts request bodies up to 32 KB


@dataclass(frozen=True)
class Entry:
    file: str
    label: str
    module: str
    summary: str
    restart: tuple = ()


ENTRIES = (
    Entry("gemini_persona.txt", "QQ · 核心人设", "qq",
          "每轮都会带上的精简人设：性格、说话分条与长度、工具使用习惯、话题禁区。", ("hakimi-agent.service",)),
    Entry("style_guide.md", "QQ · 完整风格指南", "qq",
          "完整人设与表达风格；“## 工具与数据边界”之前的部分也用于内部复盘和日记。", ("hakimi-agent.service",)),
    Entry("gemini_core_identity.txt", "QQ · 一句话身份", "qq",
          "每轮上下文里的身份声明（被问到时坦诚自己是 AI）。", ("hakimi-agent.service",)),
    Entry("owner_guidance.txt", "QQ · 对主人的语气", "qq",
          "主人说话时追加的语气指引。", ("hakimi-agent.service",)),
    Entry("owner_privilege.txt", "QQ · 主人身份说明", "qq",
          "主人发言时上下文里的身份说明。", ("hakimi-agent.service",)),
    Entry("dsh_core_identity.txt", "备用模型 · 一句话身份", "qq",
          "DeepSeek 备用模型接管时的身份声明。", ("hakimi-worker.service",)),
    Entry("dsh_runtime_prefix.txt", "备用模型 · 人设前缀", "qq",
          "DeepSeek 备用模型的人设前缀与回复格式（保留 JSON 协议说明）。", ("hakimi-worker.service",)),
    Entry("diary_writer.txt", "日记 · 写作口吻", "qq",
          "每晚写私有日记时的角色和格式。", ()),
    Entry("desktop_pet_persona.txt", "桌宠 · 人设", "pet",
          "桌宠形态的人设、说话方式和隐私规则。重启桌宠生效。", ()),
    Entry("video/hakimi_core.md", "视频 · 核心人设", "video",
          "B 站形态共用的核心人设。", ("hakimi-bilibili-watcher.service",)),
    Entry("video/bilibili_overlay.md", "视频 · B 站行为", "video",
          "看视频、发弹幕/评论时的平台规则与语气。", ("hakimi-bilibili-watcher.service",)),
)
BY_FILE = {e.file: e for e in ENTRIES}


def video_default_dir() -> Path:
    return modules.video_dir() / "personas"


def default_path(name: str) -> Path:
    if name.startswith("video/"):
        return video_default_dir() / name.split("/", 1)[1]
    return paths.PERSONA_DIR / name


def custom_path(name: str) -> Path:
    return paths.persona_overrides() / name


def _entry(name):
    entry = BY_FILE.get(name)
    if entry is None:
        raise KeyError(name)
    return entry


def read(name: str) -> dict:
    entry = _entry(name)
    custom = custom_path(name)
    default = default_path(name)
    source = custom if custom.is_file() else default
    text = source.read_text(encoding="utf-8") if source.is_file() else ""
    return {"file": name, "label": entry.label, "module": entry.module, "summary": entry.summary,
            "custom": custom.is_file(), "text": text, "restart": list(entry.restart),
            "available": default.is_file() or custom.is_file()}


def save(name: str, text: str) -> dict:
    _entry(name)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("人设内容不能为空；想恢复默认请点「恢复默认」")
    if len(text) > MAX_CHARS:
        raise ValueError(f"人设内容不能超过 {MAX_CHARS} 字")
    target = custom_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if name.startswith("video/"):
        # bili_companion reads both files from one folder: seed the sibling with its default.
        for sibling in ("hakimi_core.md", "bilibili_overlay.md"):
            other = target.parent / sibling
            if not other.exists() and (video_default_dir() / sibling).is_file():
                other.write_text((video_default_dir() / sibling).read_text(encoding="utf-8"), encoding="utf-8")
    tmp = target.with_name("." + target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, target)
    return read(name)


def reset(name: str) -> dict:
    _entry(name)
    custom_path(name).unlink(missing_ok=True)
    return read(name)


def video_persona_dir() -> Path:
    """Folder to give bili_companion: the custom one once it holds both files."""
    custom = paths.persona_overrides() / "video"
    if (custom / "hakimi_core.md").is_file() and (custom / "bilibili_overlay.md").is_file():
        return custom
    return video_default_dir()


def listing() -> list:
    rows = []
    for entry in ENTRIES:
        rows.append({"file": entry.file, "label": entry.label, "module": entry.module,
                     "custom": custom_path(entry.file).is_file(),
                     "available": default_path(entry.file).is_file() or custom_path(entry.file).is_file()})
    return rows
