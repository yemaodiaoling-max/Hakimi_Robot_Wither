"""Package layers of ``hakimi`` and the import rule between them.

    hakimi arch              # layer report
    hakimi arch --check      # exit 1 on an import that points to a higher layer
    hakimi arch --json       # modules, layers and import edges (for diagrams)

A module may import its own layer and any lower layer.  An import from a lower
layer into a higher one is allowed only when it is listed in ``EXCEPTIONS`` with
the reason; ``tests/test_architecture.py`` fails on any new upward import and on
an exception that is no longer needed, so the list can only shrink.

The rule is checked statically (``ast``), including imports inside functions.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import sys

PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Layer:
    key: str
    title: str
    summary: str
    members: tuple   # package names (``memory``) or root modules (``hakimi.paths``)


LAYERS = (
    Layer("foundation", "基础", "路径、实例配置位置、JSON 读写、时间、密钥、人设文本；不依赖任何业务包",
          ("core", "persona", "defaults", "hakimi", "hakimi._legacy", "hakimi.paths")),
    Layer("data", "原语与数据", "聊天消息原语（气泡、@、引用、文件与媒体许可、发送记录、额度）、长期记忆、关系网、网关账本",
          ("chat", "memory", "social", "gateway")),
    Layer("capabilities", "能力", "可被模型调用的单项能力：画图、数学排版、表情包、B 站解析、语音合成",
          ("tools", "voice")),
    Layer("prompting", "提示词", "把记忆、关系和能力说明组装成一轮提示词",
          ("prompt",)),
    Layer("models", "模型与检索", "模型路由与预算、各模型会话与备用通道；联网搜索、网页读取与来源记录。"
          "两包互相使用：会话调用检索工具，检索用模型做判定和 Google 落地",
          ("models", "web")),
    Layer("orchestration", "编排", "轮次调度与协调、QQ 协议通道、记忆整理（复盘）、日记/QQ 空间/专家求助等独立功能",
          ("bridge", "transport", "curation", "features")),
    Layer("entry", "入口与运维", "服务入口、巡查修复、信息面板、设置面板、命令行",
          ("ops", "console", "setup", "hakimi.config", "hakimi.modules", "hakimi.__main__",
           "hakimi.architecture", "hakimi.tools.qq_cli")),
)

# (importer, imported): why the upward import exists.  Remove an entry when its import goes away.
EXCEPTIONS = {
    ("hakimi.chat.file_delivery", "hakimi.voice.tts"):
        "发送语音文件前由语音模块确认本轮已授权（哼唱/朗读规则在 voice.tts）",
    ("hakimi.chat.interaction_policy", "hakimi.voice.tts"):
        "发送记录里把语音文件替换成语音的文字说明",
    ("hakimi.chat.interaction_policy", "hakimi.web.web_sources"):
        "回复送达时把引用的网页来源标记为已送达",
    ("hakimi.prompt.prompt_context", "hakimi.curation.personal_review"):
        "人物复盘轮次的提示词生成后标记候选已领取",
    ("hakimi.transport.official_test", "hakimi.ops.shims"):
        "初始化运行目录时写入历史入口文件 qq_cli.py",
    ("hakimi.voice.delivery", "hakimi.features.codex_help"):
        "语音发送失败时写入故障求助队列",
    ("hakimi.tools.bot_tools", "hakimi.bridge.turn_coordinator"):
        "工具调用前核对当前轮次令牌",
    ("hakimi.tools.bilibili_video", "hakimi.models.fallback_media"):
        "复用备用通道的图片压缩",
    ("hakimi.prompt.prompt_context", "hakimi.features.diary_store"):
        "把日记目录放进提示词",
    ("hakimi.models.carbon_timing", "hakimi.bridge.delivery_guard"):
        "Carbon 计时读取发送守卫的默认策略",
    ("hakimi.models.fallback_supervisor", "hakimi.bridge.turn_coordinator"):
        "备用通道通过轮次协调器领取和提交回复",
    ("hakimi.models.model_trial", "hakimi.bridge.turn_coordinator"):
        "模型试用通过轮次协调器提交",
    ("hakimi.models.vertex_session", "hakimi.bridge.turn_coordinator"):
        "主模型会话通过轮次协调器提交回复",
    ("hakimi.models.vertex_session", "hakimi.tools.qq_cli"):
        "复用 qq_cli 的图片校验",
    ("hakimi.models.fallback_commit", "hakimi.features.diary_store"):
        "备用通道提交日记导出请求",
    ("hakimi.models.fallback_runner", "hakimi.features.diary_store"):
        "校验备用通道返回的日记导出参数",
    ("hakimi.models.fallback_supervisor", "hakimi.features.codex_help"):
        "备用通道把难题转给专家求助队列",
    ("hakimi.models.gateway_text", "hakimi.features.codex_help_runner"):
        "辅助文本任务可选 Codex 后端",
    ("hakimi.features.diary_daily", "hakimi.ops.cloud_service"):
        "日记任务复用服务入口的环境和日志（ops.cloud_service 有未提交修改，暂不改动）",
}


# Modules that still import a historical path (served by hakimi._legacy) because other work on
# them is in progress; switch them to the current path together with that work, then delete the entry.
PENDING_PATH_UPDATES = {
    "hakimi.ops.cloud_service": "personal_review / review_runtime 已从 memory 移到 curation",
}


def _layer_index():
    index = {}
    for number, layer in enumerate(LAYERS):
        for member in layer.members:
            index[member] = number
    return index


def module_name(path: Path, src: Path = PACKAGE_DIR) -> str:
    rel = path.relative_to(src).with_suffix("")
    parts = ["hakimi", *rel.parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def layer_of(module: str, index=None) -> int | None:
    index = _layer_index() if index is None else index
    probe = module
    while probe:
        if probe in index:
            return index[probe]
        if probe.count(".") == 1:   # hakimi.<package>
            return index.get(probe.split(".", 1)[1])
        probe = probe.rpartition(".")[0]
    return None


def _files(src: Path = PACKAGE_DIR):
    return {module_name(p, src): p for p in sorted(src.rglob("*.py")) if "__pycache__" not in p.parts}


def scan_raw(src: Path = PACKAGE_DIR):
    """[(importer, dotted name as written, lineno, lazy)] for every ``hakimi`` import."""
    from hakimi._legacy import MODULE_ALIASES
    files = _files(src)
    rows = []
    for module, path in files.items():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), str(path))
        top = {id(node) for node in tree.body}
        is_pkg = path.name == "__init__.py"
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = module.split(".") if is_pkg else module.split(".")[:-1]
                    parts = parts[: len(parts) - (node.level - 1)]
                    base = ".".join(parts + ([base] if base else []))
                for alias in node.names:
                    sub = f"{base}.{alias.name}"
                    targets.append(sub if sub in files or sub in MODULE_ALIASES else base)
            rows += [(module, t, node.lineno, id(node) not in top) for t in targets if t.startswith("hakimi")]
    return rows


def scan(src: Path = PACKAGE_DIR):
    """{module: path} and [(importer, imported module, lineno, lazy)]; historical paths resolve to their module."""
    from hakimi._legacy import MODULE_ALIASES
    files = _files(src)
    edges = []
    for module, target, line, lazy in scan_raw(src):
        target = MODULE_ALIASES.get(target, target)
        while target and target not in files:
            target = target.rpartition(".")[0]
        if target and target != module:
            edges.append((module, target, line, lazy))
    return files, edges


def check(src: Path = PACKAGE_DIR):
    """(unlayered modules, upward imports not in EXCEPTIONS, EXCEPTIONS no longer used)."""
    index = _layer_index()
    files, edges = scan(src)
    unlayered = sorted(m for m in files if layer_of(m, index) is None)
    upward = {}
    for a, b, line, _ in edges:
        la, lb = layer_of(a, index), layer_of(b, index)
        if la is not None and lb is not None and lb > la:
            upward.setdefault((a, b), line)
    violations = sorted((a, b, line) for (a, b), line in upward.items() if (a, b) not in EXCEPTIONS)
    stale = sorted(pair for pair in EXCEPTIONS if pair not in upward)
    return unlayered, violations, stale


def graph(src: Path = PACKAGE_DIR):
    index = _layer_index()
    files, edges = scan(src)
    modules = {}
    for name, path in files.items():
        text = path.read_text(encoding="utf-8-sig")
        doc = ast.get_docstring(ast.parse(text)) or ""
        modules[name] = {"path": str(path.relative_to(src.parent)).replace("\\", "/"),
                         "layer": LAYERS[layer_of(name, index)].key if layer_of(name, index) is not None else None,
                         "lines": text.count("\n") + 1, "doc": doc.strip().split("\n")[0]}
    seen, out = set(), []
    for a, b, _, lazy in edges:
        if (a, b) not in seen:
            seen.add((a, b))
            out.append({"from": a, "to": b, "lazy": lazy,
                        "upward": (layer_of(b, index) or 0) > (layer_of(a, index) or 0),
                        "reason": EXCEPTIONS.get((a, b))})
    return {"layers": [{"key": l.key, "title": l.title, "summary": l.summary, "members": list(l.members)} for l in LAYERS],
            "modules": modules, "edges": out}


def report(src: Path = PACKAGE_DIR) -> str:
    index = _layer_index()
    files, _ = scan(src)
    lines = []
    for number, layer in reversed(list(enumerate(LAYERS))):
        members = [m for m in files if layer_of(m, index) == number]
        size = sum(files[m].read_text(encoding="utf-8-sig").count("\n") + 1 for m in members)
        lines.append(f"L{number} {layer.title}（{layer.key}）  {len(members)} 个模块 · {size} 行")
        lines.append(f"    {', '.join(layer.members)}")
        lines.append(f"    {layer.summary}")
    unlayered, violations, stale = check(src)
    lines.append(f"\n跨层例外 {len(EXCEPTIONS)} 条；新的向上依赖 {len(violations)} 条；已失效的例外 {len(stale)} 条")
    lines += [f"  未分层：{m}" for m in unlayered]
    lines += [f"  向上依赖：{a} -> {b}（第 {n} 行）" for a, b, n in violations]
    lines += [f"  可删除的例外：{a} -> {b}" for a, b in stale]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hakimi arch", description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.json:
        json.dump(graph(), sys.stdout, ensure_ascii=False, indent=1)
        return 0
    print(report())
    if args.check:
        unlayered, violations, stale = check()
        return 1 if unlayered or violations or stale else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
