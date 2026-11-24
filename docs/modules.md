# 模块说明

哈基米由一个**记忆内核**和三个可选模块组成。启用的模块记在实例目录的 `modules.json`，由设置面板或 `hakimi modules enable/disable` 修改。

```
                 ┌──────────── 记忆内核 core ────────────┐
                 │ 记忆库 memory.sqlite3 · 关系网 · 日记  │
  QQ 机器人 ───▶ │ 信息面板 / 设置面板 (:18768)           │ ◀─── 桌宠（Windows）
                 │ 本地模型网关 (:18765) ──▶ Gemini API   │
                 └───────────────▲───────────────────────┘
                                 │ 每 30 分钟交换摘要
                           视频机器人（B 站）
```

| 模块 | 安装 | 服务（systemd 用户单元） | 依赖其它模块？ |
|---|---|---|---|
| core | `pip install -e .` | `hakimi-gateway`、`hakimi-memory-console`；装了 `[vector]` 时加 `hakimi-memory-embedding` | — |
| qq | `pip install -e .[qq]` | `hakimi-worker`（收发）、`hakimi-agent`（生成回复）、`hakimi-diary.timer`；仅当在设置中显式开启运维插件时加 `hakimi-help`、`hakimi-terra-watch.timer` | 否 |
| video | `pip install -e .[video] -e ./bili_companion[auth]` + `ffmpeg` | `hakimi-bilibili-watcher`、`hakimi-memory-bridge.timer` | 否（没装 QQ 时摘要只写入内核记忆） |
| pet | Windows 上 `pip install -e .[pet]` | 无常驻服务：桌宠启动时按需拉起 `desktop_pet_agent.py` | 否 |

`deploy/install.sh --modules ...` 会按上表安装依赖并注册服务；`hakimi modules units` 打印当前启用模块需要的服务。

## 共享记忆是怎么工作的

- **QQ 机器人**直接读写内核记忆库（`<实例>/official_test_runtime/state/memory.sqlite3`）。
- **视频机器人**有自己的观看记忆库；`hakimi-memory-bridge` 每 30 分钟把“真实看过的内容”整理成摘要写进内核，并把 QQ 侧明确允许共享的知识带回 B 站侧。私聊原文、账号信息不会跨端复制。
- **桌宠**的对话和屏幕经历以 `channel=desktop_pet` 写进内核记忆，挂在主人的私聊会话下；在群聊里引用时会过滤私密内容。

## 可选组件

| 组件 | 用途 | 怎么启用 |
|---|---|---|
| 语义检索 | 记忆按语义相似度召回（否则用全文检索） | `install.sh --vector` |
| 备用兜底模型（默认关） | 主模型超时/不可用时接管 QQ 回复 | 在设置面板启用并选择「直接 HTTP API」（零额外依赖）或可选 `dsh` 命令行 |
| 独立复盘 / QQ 备用切换（默认复用或关闭） | 为记忆复盘或主模型故障热切换配置独立供应商 | 在设置面板「高级与独立 API」或 `aux_config.json` 中启用并配置 |
| 巡查、Terra 督导、专家求助、Sol 修复（默认全关） | 服务看门狗、风格督导、问题分析与自动修复 | 在设置面板「高级与独立 API」或 `aux_config.json` 中显式启用；后端可选主网关、独立 HTTP API 或 `codex` CLI |

## 只装一个模块时

- 只装 **qq**：视频活动页自动隐藏，B 站相关工具（看 B 站链接）仍可用，需要在面板里登录 B 站。
- 只装 **video**：消息日志、群成员匹配页隐藏；视频经历照常进入内核记忆，可在关系面板/记忆库里查看。
- 只装 **pet**：桌宠的记忆、日记照常工作；QQ 群相关的上下文为空。
