"""功能登记表：哈基米有哪些功能、由哪些模块实现、开放哪些工具、靠什么开关。

    hakimi features              # 每项功能及当前实例里的开关状态
    hakimi features --markdown   # 生成 docs/features.md

新增功能时在 FEATURES 里登记一项；工具本身的说明仍在 hakimi/prompt/tool_catalog.py。
"""
import argparse
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from hakimi import paths


@dataclass(frozen=True)
class Switch:
    """How to tell whether a feature is on in an instance."""
    kind: str            # "config" | "file" | "absent" | "service" | "timer"
    target: str          # config: "runtime:config.json:fallback.enabled"; file: "root:state/diary_mode.json"; service: unit
    label: str = ""


@dataclass(frozen=True)
class Feature:
    key: str
    name: str
    summary: str
    modules: tuple
    tools: tuple = ()
    switches: tuple = ()
    notes: str = ""
    extra: dict = field(default_factory=dict)


FEATURES = (
    Feature("official_lane", "QQ 官方机器人通道", "收发群聊/私聊消息、图片与文件，管理回复窗口和群路由",
            ("transport.official_transport", "transport.official_routes", "transport.official_api", "transport.official_lane",
             "transport.official_files", "transport.official_groups"),
            ("members.mention",), (Switch("service", "hakimi-worker"), Switch("config", "runtime:official_lane_config.json:allow_private_chat", "私聊"))),
    Feature("scheduling", "对话调度", "决定何时开口：被@即时唤醒、上下文积累、相关续聊、每轮回复后的45秒评分窗口、活跃群退让",
            ("bridge.conversation_scheduler", "bridge.bridge_daemon", "bridge.participation_policy", "bridge.turn_coordinator"),
            ("reply.send", "reply.skip", "context.read", "turn.progress", "turn.failure"),
            (Switch("config", "runtime:config.json:conversation_scheduler.enabled"), Switch("config", "runtime:config.json:participation_policy.enabled", "主动参与"))),
    Feature("user_reply_quota", "按用户控制回复成本", "同一用户滚动30分钟最多7轮，Gemini与DSH共用，分条回复只计一轮",
            ("bridge.user_reply_quota", "bridge.turn_coordinator", "bridge.bridge_daemon"),
            notes="发布任务前检查，提交回复时按实际回复对象复查；历史随turn_state持久化，跨群及私聊共用同一user_id额度。"),
    Feature("cold_topics", "冷场主动抛话题", "群里长时间安静时，按配置窗口主动开启话题",
            ("bridge.cold_topics",), (), (Switch("config", "runtime:config.json:lurker_schedule.proactive_topic.enable"),)),
    Feature("gemini", "Gemini 主回复", "经私有 Vertex 网关直连 Gemini，按群保留会话，月度用量由管理员手动控制",
            ("models.vertex_route", "models.vertex_session", "models.scoped_session", "models.provider_health", "ops.cloud_service"),
            ("web.search", "web.read", "web.sources", "time.now", "calculator.evaluate", "tools.list", "runtime.status"),
            (Switch("service", "hakimi-agent"), Switch("config", "root:state/vertex_route.json:enabled", "Vertex 直连"))),
    Feature("dsh_fallback", "DSH 兜底", "Gemini 超时或不可用时由 DSH 接管，同一套工具与回执",
            ("models.fallback_supervisor", "models.fallback_runner", "models.fallback_prompt", "models.fallback_commit", "models.dsh_tools"),
            ("image.read", "bots.propose"), (Switch("config", "runtime:config.json:fallback.enabled"),)),
    Feature("voice", "QQ语音", "本地CPU转写、会话私有原文与明确请求时的原创音色/短哼唱",
            ("voice.store", "voice.worker", "voice.delivery", "voice.tts"),
            ("voice.read", "voice.speak", "voice.hum"),
            (Switch("service", "hakimi-voice"), Switch("config", "root:voice_config.json:enabled"), Switch("config", "root:voice_config.json:tts_enabled", "明确请求发声"))),
    Feature("memory", "长期记忆", "原文存储、全文+语义混合检索、按群范围隔离，笔记原文不可改写",
            ("memory.memory_store", "memory.store_write", "memory.store_retrieval", "memory.smart_memory", "memory.memory_lifecycle", "tools.memory_result"),
            ("memory.search", "memory.read", "memory.remember"), (Switch("service", "hakimi-memory-embedding", "语义向量服务"),)),
    Feature("daily_events", "日常事件卡", "理解 API 在既有人物笔记批次中整理少量真实事件，原文清理后保留短摘要与向量",
            ("memory.daily_events", "memory.impression_events", "memory.relationship_events", "memory.post_reply_review", "memory.review_runtime", "memory.review_audit", "memory.relationship_history", "memory.store_maintenance"),
            notes="真实回复窗口结束后异步复盘，独立单槽调用视频理解 API，请求 Gemini 3.8 Flash low；保留有界短审核理由与来源，区分事实、关系与原话角色。Gemini 继续书写人物印象。"),
    Feature("social", "关系网", "人物小本子、好感/亲近评分（来源分开记录）、互动熟悉度、记忆图谱",
            ("social.memory_social", "social.social_record", "social.social_query", "social.social_behavior", "social.relation_perspective", "memory.memory_graph"),
            ("memory.social", "memory.social_append", "memory.graph"), (Switch("config", "runtime:social_behavior_policy.json:protected_user_ids", "受保护成员"),)),
    Feature("console", "记忆与关系网面板", "本机网页面板（SSH 隧道访问，令牌保护）：关系图、人物、记忆、身份关联、预算、复盘变化",
            ("console.memory_console", "console.payloads_social", "console.payloads_memory", "console.payloads_identity", "console.payloads_review_history"),
            (), (Switch("service", "hakimi-memory-console"),), "端口 18768"),
    Feature("identity", "新旧身份关联", "把旧 QQ 号记忆和官方通道成员人工关联，按关联读取旧笔记",
            ("social.identity_links", "social.identity_edit", "social.identity_legacy"), (), ()),
    Feature("personal_review", "内部复盘", "每两小时挑选最多三位成员整理印象与记忆（不发群消息）",
            ("memory.personal_review", "memory.review_select"), (), ()),
    Feature("math", "数学排版", "Markdown/TeX 公式本地排成图片发送", ("tools.math_render", "models.dsh_math"), ("math.send",), ()),
    Feature("files", "文件与聊天导出", "发送本地文件、导出聊天记录", ("transport.file_delivery", "transport.official_files"),
            ("files.list", "files.send", "chat.export"), ()),
    Feature("stickers", "表情包", "按语境从图库选图、收藏群友表情（屏蔽确认过的奶龙）",
            ("tools.sticker_policy", "tools.sticker_catalog"), ("stickers.list", "stickers.collect"),
            (Switch("config", "runtime:config.json:sticker_policy.enabled"),)),
    Feature("vision", "图片与视频理解", "读取本轮授权的图片；按需抽帧观看 B 站与 QQ 小程序视频",
            ("transport.image_permissions", "tools.bilibili_video", "models.fallback_media"),
            ("images.list", "video.watch", "video.watch_miniapp"), ()),
    Feature("diary", "私有日记", "每晚 22:30（纽约时间）写一篇日记并由 Terra 配图，可导出 PDF",
            ("features.diary_daily", "features.diary_store", "features.diary_latex"), ("diary.read", "diary.export"),
            (Switch("timer", "hakimi-diary.timer"),)),
    Feature("qzone", "QQ 空间说说", "旧版每日说说（已由私有日记取代，云端未迁移）", ("features.qzone_publisher", "features.qzone_fallback"),
            (), (Switch("absent", "root:state/diary_mode.json", "未被日记取代"),)),
    Feature("feeding", "投喂", "主人绑定后用 /投喂 记录虚拟投喂，两种模型都能读到回执", ("tools.feeding",), (), ()),
    Feature("codex_help", "Codex 专家求助", "复杂问题或故障分析时排队请 Codex 给内部分析（有额度）",
            ("features.codex_help", "features.codex_help_runner", "features.codex_help_service"), (),
            (Switch("config", "root:codex_help_config.json:enabled"), Switch("service", "hakimi-help"))),
    Feature("supervision", "健康巡查 / Terra 督导 / Sol 修复", "每 5 分钟巡查；Terra 两小时一次风格与关系评价；可确认的进程故障由 Sol 最小修复",
            ("ops.cloud_supervisor", "ops.terra_supervision", "ops.terra_budget", "ops.sol_repair"), (),
            (Switch("timer", "hakimi-terra-watch.timer"),)),
    Feature("model_trial", "模型试用", "24 小时限时试用另一个 Gemini 型号，由 Terra 对比评价后自动恢复",
            ("models.model_trial",), (), ()),
)


def _dotted(value, dotted):
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _folder(where, root):
    return root if where == "root" else root / paths.RUNTIME_NAME


def _systemd(*args):
    if not shutil.which("systemctl"):
        return None
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def switch_state(switch, root=None):
    root = Path(root) if root else paths.root()
    if switch.kind == "config":
        where, name, key = switch.target.split(":", 2)
        try:
            value = _dotted(json.loads((_folder(where, root) / name).read_text(encoding="utf-8-sig")), key)
        except (OSError, ValueError):
            return "缺配置"
        if isinstance(value, list):
            return f"{len(value)} 项"
        return {True: "开", False: "关", None: "未设置"}.get(value, str(value))
    if switch.kind in ("file", "absent"):
        where, rel = switch.target.split(":", 1)
        exists = (_folder(where, root) / rel).exists()
        return "开" if exists == (switch.kind == "file") else "关"
    state = _systemd("is-active", switch.target)
    return state or "未知"


def status(root=None):
    return [(f, [(s.label or s.target.split(":")[-1], switch_state(s, root)) for s in f.switches]) for f in FEATURES]


def markdown():
    lines = ["# 功能一览", "", "由 `hakimi features --markdown` 生成；登记表在 `src/hakimi/features/registry.py`。", ""]
    for f in FEATURES:
        lines += [f"## {f.name}（`{f.key}`）", "", f.summary + ("。" if not f.summary.endswith("。") else ""), ""]
        lines.append("- 模块：" + "、".join(f"`hakimi.{m}`" for m in f.modules))
        if f.tools:
            lines.append("- 模型可调用的工具：" + "、".join(f"`{t}`" for t in f.tools))
        if f.switches:
            lines.append("- 开关：" + "；".join(f"{s.kind} `{s.target}`" + (f"（{s.label}）" if s.label else "") for s in f.switches))
        if f.notes:
            lines.append("- 备注：" + f.notes)
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="hakimi features", description="功能登记表")
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--root")
    args = parser.parse_args(argv)
    if args.markdown:
        print(markdown())
        return 0
    for feature, switches in status(args.root):
        state = "，".join(f"{label}={value}" for label, value in switches) or "-"
        print(f"{feature.name:24s} {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
