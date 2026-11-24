"""实例配置文件登记表与检查。

配置文件仍是各实例目录里的 JSON（格式、位置不变；运行中也会被程序更新，例如接入新群时）。
这里集中说明每个文件放在哪、给谁用、谁会写，并在发布前检查格式：

    hakimi config list            # 所有配置文件
    hakimi config check           # 检查线上实例（HAKIMI_ROOT）
    hakimi config show NAME [--where runtime]

字段说明见 docs/configuration.md。
"""
import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from hakimi import paths

ID = re.compile(r"[1-9][0-9]{4,19}\Z")


@dataclass(frozen=True)
class ConfigFile:
    name: str
    where: tuple            # "root" / "runtime"
    purpose: str
    required: dict = field(default_factory=dict)   # top-level key -> type
    written_by: str = ""
    optional: bool = False


CONFIGS = (
    ConfigFile("task_routing.json", ("root",), "主聊天轮次模型选择：single/dual；生产配置为日常 Hydrogen、复杂任务 Carbon",
               {"mode": str, "single_model": str}, optional=True),
    ConfigFile("config.json", ("root", "runtime"),
               "机器人主配置：身份与群范围、投递节奏、参与策略、DSH 兜底、调度、表情包、按群回复上限",
               {"bot_profile": dict, "delivery_policy": dict, "participation_policy": dict, "fallback": dict,
                "conversation_scheduler": dict, "lurker_schedule": dict, "group_weights": dict},
               "运行目录副本由 official_test.prepare 从根目录生成，接入新群时 official_groups 会更新"),
    ConfigFile("group_profiles.json", ("root", "runtime"),
               "每个群的风格档案（语气、长度、学习群等）与群备注",
               {"version": int, "default_profile": str, "profiles": dict, "groups": dict},
               "接入新群时 official_groups 会追加"),
    ConfigFile("official_test_config.json", ("root",),
               "官方机器人通道开关与账号（app_id、运行模式、主动消息、私聊）",
               {"version": int, "enabled": bool, "app_id": str, "test_group_number": str, "operation_mode": str}),
    ConfigFile("official_lane_config.json", ("runtime",),
               "官方通道 worker 实际读取的配置（由 official_test_config.json 生成）",
               {"version": int, "app_id": str, "operation_mode": str}, "official_test.prepare"),
    ConfigFile("codex_help_config.json", ("root",),
               "Codex 专家求助的开关与调用额度", {"enabled": bool, "max_calls_per_hour": int, "max_calls_per_day": int}),
    ConfigFile("file_send_config.json", ("root",), "发文件的大小上限与上传超时",
               {"max_mb": dict, "upload_timeout_seconds": int}, optional=True),
    ConfigFile("video_tools_config.json", ("root",), "B站/小程序视频工具（ffmpeg 路径、cookies）",
               {"ffmpeg_path": str}, optional=True),
    ConfigFile("social_behavior_policy.json", ("root", "runtime"),
               "关系网行为策略：受保护成员（不受亲疏影响）", {"version": int, "protected_user_ids": list}),
    ConfigFile("social_seed.json", ("root", "runtime"), "人工审阅过的关系种子（一次性导入）",
               {"version": int, "people": list, "relations": list}, optional=True),
    ConfigFile("official_memory_review.json", ("root",), "旧 QQ 记忆迁移的人工审阅结果",
               {"version": int, "group_reviews": dict}, optional=True),
)
BY_NAME = {c.name: c for c in CONFIGS}


def folder(where, root=None):
    root = Path(root) if root else paths.root()
    return root if where == "root" else root / paths.RUNTIME_NAME


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def load(name, base, default=None):
    """Instance file in ``base``; else the packaged default; else ``default``."""
    for candidate in (Path(base) / name, paths.default_config(name)):
        try:
            return read(candidate)
        except FileNotFoundError:
            continue
    return default


def _semantic(config, where, data):
    problems = []
    if config.name == "config.json":
        profile = data.get("bot_profile", {})
        for key in ("active_groups", "ignored_groups", "random_peek_groups", "low_frequency_groups", "ignored_user_ids"):
            bad = [v for v in profile.get(key, []) if not ID.fullmatch(str(v))]
            if bad:
                problems.append(("error", f"bot_profile.{key} 含非数字ID：{bad[:3]}"))
        bad = [k for k in data.get("group_weights", {}) if not ID.fullmatch(str(k))]
        if bad:
            problems.append(("error", f"group_weights 的群号格式无效：{bad[:3]}"))
        if where == "runtime" and not data.get("official_lane"):
            problems.append(("warning", "运行目录配置没有 official_lane=true（不是由 prepare 生成？）"))
    if config.name == "group_profiles.json":
        profiles = data.get("profiles", {})
        for key in ("default_profile", "private_profile"):
            if data.get(key) and data[key] not in profiles:
                problems.append(("error", f"{key}={data[key]!r} 不在 profiles 中"))
        missing = sorted({g.get("profile") for g in data.get("groups", {}).values()
                          if isinstance(g, dict) and g.get("profile") and g["profile"] not in profiles})
        if missing:
            problems.append(("error", f"groups 引用了不存在的风格档案：{missing}"))
    if config.name in ("official_test_config.json", "official_lane_config.json"):
        if not re.fullmatch(r"[0-9]{6,20}", str(data.get("app_id", ""))):
            problems.append(("error", "app_id 应为数字"))
        if data.get("operation_mode") not in {"test", "normal"}:
            problems.append(("error", "operation_mode 只能是 test 或 normal"))
    if config.name == "social_behavior_policy.json":
        bad = [v for v in data.get("protected_user_ids", []) if not ID.fullmatch(str(v))]
        if bad:
            problems.append(("error", f"protected_user_ids 含无效ID：{bad[:3]}"))
    if config.name == "codex_help_config.json":
        bad = [k for k, v in data.items() if k != "enabled" and isinstance(v, (int, float)) and v < 0]
        if bad:
            problems.append(("error", f"额度不能为负：{bad}"))
    if config.name == "task_routing.json":
        from hakimi.models.task_routing import validate
        try:
            validate(data)
        except ValueError as exc:
            problems.append(("error", str(exc)))
    return problems


def check(root=None):
    """[(level, file, message)] for every registered file of an instance."""
    findings = []
    for config in CONFIGS:
        for where in config.where:
            path = folder(where, root) / config.name
            label = f"{where}/{config.name}"
            if not path.is_file():
                findings.append(("info" if config.optional else "error", label, "文件不存在"))
                continue
            try:
                data = read(path)
            except (OSError, ValueError) as exc:
                findings.append(("error", label, f"不是有效 JSON：{exc}"))
                continue
            if not isinstance(data, dict):
                findings.append(("error", label, "顶层应为对象"))
                continue
            for key, kind in config.required.items():
                if key not in data:
                    findings.append(("error", label, f"缺少字段 {key}"))
                elif not isinstance(data[key], kind) or (kind is int and isinstance(data[key], bool)):
                    findings.append(("error", label, f"{key} 应为 {kind.__name__}，实际 {type(data[key]).__name__}"))
            findings += [(level, label, message) for level, message in _semantic(config, where, data)]
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hakimi config", description="实例配置文件登记与检查")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    c = sub.add_parser("check"); c.add_argument("--root")
    s = sub.add_parser("show"); s.add_argument("name"); s.add_argument("--where", default="root"); s.add_argument("--root")
    args = parser.parse_args(argv)
    if args.command == "list":
        for config in CONFIGS:
            print(f"{config.name:30s} {'+'.join(config.where):13s} {config.purpose}")
        return 0
    if args.command == "show":
        print(json.dumps(read(folder(args.where, args.root) / args.name), ensure_ascii=False, indent=2))
        return 0
    findings = check(args.root)
    for level, label, message in findings:
        print(f"[{level}] {label}: {message}")
    errors = sum(1 for f in findings if f[0] == "error")
    print(f"{len(CONFIGS)} 个配置文件，{errors} 个错误，{sum(1 for f in findings if f[0] == 'warning')} 个警告")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
