# 哈基米 Hakimi

**简体中文** · [English](README.en.md)

一只住在 QQ 群、B 站和你电脑桌面上的猫系 AI 同伴。三种形态共用**同一个记忆内核**：在 QQ 群里聊过的事、在 B 站看过的视频、桌宠陪你写代码的经历，都会成为同一只哈基米的记忆，并在下次聊到相关话题时自然想起来。

- ⚡ **单 API 开箱即用，也可按需拆分多 API / 多 Agent**：开源版只需填写**一个 API Key**（支持 Google Gemini、Gemini 兼容或任意 OpenAI 兼容接口）即可跑通完整功能；备用模型、异步复盘、上下文筛选、联网预搜索与运维督导均支持在设置面板独立配置，默认全部关闭或复用主网关。
- 🐱 **会挑时机、会合并追问的群友**：被 @ 或私聊立即回应并短暂收齐补充追问；支持展开嵌套转发消息与智能引用；其它时候先读上下文再决定要不要接话，冷场时主动抛话题，热闹时懂得退让。
- 🧠 **分层记忆内核与分级遗忘**：原文存储 + 全文检索（FTS5）+ 可选本地稠密向量语义检索（`bge-small-zh-v1.5`），按群隔离、维护人物小本子与关系图谱、异步复盘并每晚写私有日记；过期的跨端视频记忆按深度分级到期并物理清理。
- 📺 **自己刷 B 站并与 QQ/桌宠联动**：按兴趣刷推荐视频、盯守指定直播间；看片经历经自传快照注入共享记忆内核，仅在聊到相关内容时按话题召回（无关群聊**零额外提示词开销**），实时状态供桌宠与 QQ 询问读取。
- 🖥️ **桌面宠物**：趴在 Windows 桌面上看屏幕吐槽、语音对话、在你确认后帮你操作电脑，实时同步 B 站近况，支持固定交接文件的长班次轮转机制。
- 🔐 **密钥只在你的服务器上**：所有主模型请求经本机 HTTPS 网关统一转发、记账与限额；辅助 API Key 与 B 站 Cookie 单独加密保存。

## 目录

- [模块](#模块) · [架构](#架构) · [单 API 开箱与独立模块开关](#单-api-开箱与独立模块开关) · [功能一览](#功能一览)
- [快速开始（Linux 服务器）](#快速开始linux-服务器) · [桌宠（Windows）](#桌宠windows)
- [自定义人设与身份](#自定义人设与身份) · [常用命令](#常用命令) · [更新](#更新)
- [数据与隐私](#数据与隐私) · [开发与测试](#开发与测试) · [文档](#文档) · [许可](#许可)

## 模块

由一个必装的**记忆内核**和三个可选模块组成，可以任意组合：只装一个、两个或全部都行。没启用的模块不会启动，也不会影响其它模块。

| 模块 | 做什么 | 运行在 |
|---|---|---|
| **记忆内核** `core`（必装） | 长期记忆（SQLite + FTS5，可选 `bge-small-zh-v1.5` 语义检索）、关系图谱与好感度、异步复盘与日记、信息/设置面板、本地模型网关 | Linux 服务器（也可在 Windows 上运行） |
| **QQ 机器人** `qq` | 官方 QQ 通道（含 OneBot 适配）：群聊/私聊、追问合并、嵌套转发展开、冷场主动发言、可追溯联网检索、表情包、公式排图、音视频与图片理解、可选 AI 绘图与本地 QQ 语音、每日日记 | 同上 |
| **视频机器人** `video` | `bili_companion`：按兴趣刷 B 站推荐、蹲守优先直播间、可选发弹幕和评论；生成带校验的观片自传与实时在场状态并入共享记忆 | Linux 服务器（需要 `ffmpeg`） |
| **桌宠** `pet` | Windows 桌面宠物：看屏幕吐槽、语音对话、确认后代操作电脑、实时查看 B 站近况，并通过固定交接文件维持长时值守与记忆同步 | Windows 电脑 |

## 架构

### 系统拓扑与同魂联动

```
                       ┌──────────────────── 记忆内核 core ────────────────────┐
                       │  memory.sqlite3 (FTS5 + 可选 bge-small-zh 向量)       │
  QQ 机器人 (qq) ────▶ │  关系图谱 · 异步复盘 (curation) · 私有日记            │ ◀─── 桌宠 (pet，Windows 本机或 SSH)
  (官方通道 / OneBot)  │  信息与设置面板 (:18768，仅回环 + 令牌)               │      看屏幕 · 语音 · 读近况 · 固定交班
                       │  本地 HTTPS 网关 (:18765，ledger) ─▶ Gemini / OpenAI  │
                       └────────────────────▲────────────────────────────┬─────┘
                                            │ hakimi-memory-bridge.timer │ 可选扩展（aux_config.json /
                                            │ 每 30 分钟双向校验交换摘要   │         antigravity_tasks.json）
                                 视频机器人 (video，bili_companion)        └─▶ 独立 HTTP API · 可选 CLI Agent
                                 ├─ 输出至内核：自传手记 (仅话题召回 + 分级清理) + 实时 presence
                                 └─ 接收自内核：去私聊脱敏后的 QQ/桌宠兴趣线索（严禁 self:bilibili 自回环）
```

- **同一套代码，纯配置区分**：下载开源版直接运行，与开启多路高可用、多 Agent 协作的生产实例使用的是同一套核心代码；所有行为差异和独立端点都在**实例目录**（默认 `~/hakimi-data`）中通过配置文件开启。
- **跨端自传桥与严格控本**：
  - 视频端通过 SHA-256 校验的 JSON 快照输出观片/直播自传（同步为 `self:bilibili` 共享记忆，仅在聊天话题真正相关时触发检索召回，普通无关 QQ 群聊不注入、不额外消耗 token）与轻量实时 `presence.json`（QQ 仅在主人交互或被主动问及近况时注入超短状态；桌宠自动携带完整近况）。
  - 分级遗忘与物理删盘：视频与直播记忆按观片深度/互动程度设定差异化过期时间（高兴趣 45–60 天、普通 14 天、低兴趣 5 天，默认上限 240 条），到期或超限后连同 FTS 索引、向量与孤立图谱边一并物理删除；`bili_companion` 侧同步按日清理陈旧证据与已确认队列。
- **开箱只需一个上游 API**：所有核心调用经本机网关 `https://127.0.0.1:18765` 转发并记录于 `gateway_ledger.sqlite3`，支持 Google Gemini、Gemini 兼容端点或任何 OpenAI Chat Completions 兼容接口（如 DeepSeek、OpenRouter、自建网关等）。

### 代码分层与依赖方向

主包 `hakimi`（`src/hakimi/`）按**七层单向依赖**组织，下层不依赖上层；可通过 `hakimi arch --check` 与 `tests/test_architecture.py` 进行静态 AST 检查：

| 层级 | 包含包 / 模块 | 职责与说明 |
|---|---|---|
| **L6 入口与运维** `entry` | `ops`、`console`、`setup`、`config`、`modules`、`__main__`、`architecture`、`tools.qq_cli` | systemd 服务入口、健康巡查与最小修复、信息面板、设置面板、命令行入口与分层检查 |
| **L5 编排** `orchestration` | `bridge`、`transport`、`curation`、`features` | 轮次调度与追问合并（含可选 C++17 扩展 `bridge/followup.cpp`）、官方 QQ/OneBot 通道、异步记忆与人物复盘、日记/旧空间/专家求助 |
| **L4 模型与检索** `models` | `models`、`web` | 多档模型路由与预算、主/热备/兜底/Antigravity 会话；联网公开搜索与抓取、原生 Google Grounding 计费接入、可选 DSH 搜索与来源归档 |
| **L3 提示词** `prompting` | `prompt` | 上下文压缩与预算分配、分群语气档案、工具目录与按需筛选、门控注入 B 站经历与在场状态 |
| **L2 能力** `capabilities` | `tools`、`voice` | 多模型 AI 绘图、Markdown/TeX 公式排图、表情包、B 站与 QQ 小程序抽帧、投喂、可选本地 CPU 语音识别与合成 |
| **L1 原语与数据** `data` | `chat`、`memory`、`social`、`gateway` | 跨层共用的消息原语（气泡、@、引用、媒体/文件许可、配额、发送记录）、`MemoryStore` 与自传同步/清理、关系图谱与跨群身份、网关账本 |
| **L0 基础** `foundation` | `core`、`persona`、`defaults`、`paths`、`_legacy` | `root` / `runtime` / `base` 三级路径解析、JSON 读写、时间、Fernet 密钥、人设文本及旧模块路径兼容别名 |

视频机器人位于同仓库 `bili_companion/`，Windows 桌宠位于 `desktop_pet/`（根目录 `desktop_pet_agent.py` 为兼容入口）。

## 单 API 开箱与独立模块开关

为了既能“填一个 Key 就能用”，又兼容生产环境给不同职责挂不同模型与外部 Agent，所有辅助能力统一由实例目录下的 `aux_config.json` 与 `/setup.html` 面板的 **「高级与独立 API」** 管理：

| 模块 / 辅助能力 | `aux_config.json` 键 | 开源默认（开箱） | 可选独立模式 |
|---|---|---|---|
| **异步复盘与日常事件卡** | `review` | `mode: "main_gateway"`（复用主网关） | `custom_api`（独立 OpenAI/Gemini HTTP API）、`companion_duty` 或关闭 |
| **QQ 主模型热切换通道** | `qq_backup` | `enabled: false`（`mode: "disabled"`） | `custom_api`（独立 OpenAI/Gemini HTTP API，支持 `fallback_model`）或 `companion_duty` |
| **主模型故障兜底** | `fallback` | `enabled: false`（`backend: "http_api"`） | 开启后走独立 HTTP API（无需外部 CLI）或外部 `dsh` CLI |
| **上下文与续聊筛选裁判** | `context_judge` | `mode: "heuristic"`（本地启发式，零调用） | `main_gateway`、`custom_api` 或 `dsh` |
| **联网预搜索与付费搜索** | `search_routing` | `enabled: false`，`judge_mode: "local_rules"`，`paid_search_mode: "disabled"` | 可启用预搜索判断（`main_gateway` / `custom_api` / `dsh`）与 DeepSeek 付费搜索；可独立开启 `dsh_on_budget`，当搜索预留不足或服务商限流时使用 DSH 账号检索（可能另计费）。默认关闭，公共来源仍可用 |
| **每日日记兜底写手** | `diary.fallback_enabled` | `false`（仅主模型，不依赖外部兜底） | `true`（主模型不可用时调用已启用的 `fallback` 通道） |
| **运维看门狗 / Terra 督导 / Sol 修复 / 专家求助** | `ops.*` | 4 项开关全部 `false`（零额外后台督导进程） | 逐项独立开关（`watchdog_enabled`、`terra_supervision_enabled`、`sol_repair_enabled`、`codex_help_enabled`），可选 `main_gateway`、`custom_api` 或 `codex_cli` |

> 普通用户保持默认配置即可运行全部聊天、记忆、日记、搜图、公式排版等核心体验；Linux 环境还可在 `<实例目录>/antigravity_tasks.json` 为复杂 QQ 问答（`complex`）、人物记忆反思（`reflection`）与日记（`diary`）可选配置 Antigravity CLI 路由。如需给某一项挂单独的低价模型或外部 CLI，在设置面板展开 **「高级与独立 API」** 填写并保存即可，配置格式可通过 `hakimi config check` 校验。详细配置键见 [docs/configuration.md](docs/configuration.md)。

## 功能一览

**对话与调度**
- 被 @ 或私聊即时回应，并在短暂缓冲窗口内通过可选 C++17 分类器/规则引擎合并同一发言人的连续追问与补充；支持逐层展开 QQ 嵌套转发消息。
- 平时积累上下文、给每轮发言打分后再决定是否插话，活跃群主动退让；发送前自动检查插话情况并智能附带目标引用；冷场时按配置窗口主动开启话题。
- 按用户限流：同一个人滚动 30 分钟内最多触发 7 轮回复（主/备通道共用），防刷屏、控成本。

**记忆、关系与日记**
- 原文存储，SQLite FTS5 + 可选本地稠密向量（`bge-small-zh-v1.5`）混合检索；按群严格隔离，群聊里绝不泄露私聊内容。
- 人物小本子、好感/亲近评分（来源分开记录）、互动熟悉度与记忆图谱；`hakimi.curation` 在回复窗口结束后异步整理事件卡和人物印象，并每两小时安静复盘部分成员。
- B 站观片/直播自传与共享记忆打通，按相关度自动唤起“B站亲历”并按梯度过期物理清理。
- 每晚 22:30（纽约时间）写一篇私有日记并支持配图、LaTeX 精美排版、PDF 导出与已验证主人私聊送达。

**联网检索与来源追溯**
- 公开网页搜索与受限 HTTPS 分页读取（含新闻/百科补救与番剧日历），不绕过验证码或付费墙，并在触发禁网约束时严格遵守。
- 每轮检索结果生成与会话绑定的不可变来源快照（`[W1]`、`web.sources`），群友后续引用回复时可离线回溯真实引用依据。
- 可选配置原生 Google Search Grounding 计费接入、DeepSeek 付费搜索及 `dsh_on_budget` 降级通道。

**多媒体与语音**
- 看图（只读本轮授权的图片）、按需抽帧观看 B 站与 QQ 小程序视频。
- Markdown/TeX 公式在本地排版成图片发送；按语境挑表情包并收藏群友表情。
- 可选 AI 绘图：默认关闭，在面板「绘图设置」配置模型列表与按日/月预算上限后启用。
- 可选 QQ 语音（`hakimi.voice`，默认关闭）：支持本地 CPU 语音转写（仅本轮会话可见，原文不入共享长期记忆）与明确请求时的本地语音合成/短哼唱（仓库不含他人音色样本，详情见 [docs/voice.md](docs/voice.md)）。

**面板与运维**（面板只监听本机 `127.0.0.1:18768`，令牌保护，通过 SSH 隧道访问）
- 关系面板（图谱、人物、跨群档案）、消息日志（活动页同步展示回复通道、触发原因与模型档位）、旧群与成员匹配、视频活动、复盘变化、调用预算、绘图设置与设置面板 `/setup.html`。
- 本地模型网关统一记账与月度花费拦截；可选健康看门狗、Terra 督导、Sol 最小修复与专家求助队列默认全关，可按需开启。

完整清单与每项功能的开关见 [docs/features.md](docs/features.md)。

## 快速开始（Linux 服务器）

需要 Python 3.12+ 和 git；视频模块另需 `ffmpeg`。已在 Ubuntu / Debian 上测试（安装 `hakimi` 时若存在 C++17 编译器会自动编译加速扩充追问模块 `_followup_rules`，缺少编译器时自动无感退回纯 Python 实现）。

```bash
git clone https://github.com/yemaodiaoling-max/Hakimi_Robot_Wither.git hakimi && cd hakimi
deploy/install.sh --modules qq,video,pet      # 只要 QQ 机器人就写 --modules qq
# 想要本地语义检索（约 500 MB）可以加 --vector
```

安装脚本会创建虚拟环境、实例目录（默认 `~/hakimi-data`）、本地网关证书和面板令牌，并以 systemd 用户服务启动**网关**和**面板**。脚本本身不会询问任何密钥。

然后打开设置面板：

```bash
# 在你自己的电脑上建立隧道（面板只监听服务器本机）
ssh -L 18768:127.0.0.1:18768 用户@服务器
# 在服务器上打印带令牌的地址
HAKIMI_ROOT=~/hakimi-data .venv/bin/hakimi setup url
```

在浏览器打开打印出的 `http://127.0.0.1:18768/setup.html#token=...`，依次填写：

1. **模块**：勾选要用的模块。
2. **模型与 Key**：填 [Google AI Studio](https://aistudio.google.com/apikey) 的 Gemini API Key，或选择「OpenAI 兼容地址」填写任意兼容接口（DeepSeek / OpenRouter / 本地模型等），点「检测」读取可用模型并设置主/轻量模型即可。
3. **主人与人设**：机器人名字、你的称呼和 ID。
4. **QQ 机器人**：在 [QQ 开放平台](https://q.qq.com) 创建机器人，填 AppID、AppSecret 和测试群号。
5. **B 站**：扫码登录（或手动粘贴 Cookie），可选一个优先直播间。
6. **桌宠**：选择本机或 SSH 连接，复制生成的 `pet_connection.json`。
7. **（可选）高级与独立 API**：如果只想单 API 运行可直接跳过；需要给复盘、QQ 热备、兜底模型、裁判或运维督导指定独立 API/Agent 时再展开配置。

勾选「保存后重启相关服务」并保存，面板会把设置写进各模块的配置文件并启动对应服务。检查运行状态：`systemctl --user status 'hakimi-*'`。

> 想让机器人在你退出 SSH 后继续运行：`sudo loginctl enable-linger $USER`。

## 桌宠（Windows）

```powershell
git clone https://github.com/yemaodiaoling-max/Hakimi_Robot_Wither.git hakimi; cd hakimi
python -m venv .venv; .venv\Scripts\pip install -e .[pet]
# 把设置面板「桌宠」一节生成的内容保存为 desktop_pet\pet_connection.json
.venv\Scripts\python desktop_pet\pet_app.py
```

机器人装在同一台 Windows 电脑上时选“本机”，桌宠会直接在本机启动后端；装在服务器上时选“SSH”，桌宠通过一条常驻的 SSH 连接和服务器通信。桌宠对电脑的每一个操作（打开软件、运行命令、键鼠操作）都会先弹出确认按钮，你点「允许」后才会执行；长期值守时可通过 `desktop_pet\pet_cli.py bili-status` 查看 B 站近况，并通过固定交接文件与 `rotate` / `handoff-sync` 命令实现低上下文轮班交接。

## 自定义人设与身份

人设和提示词都是文本文件，不需要改代码：

- **人设**：在设置面板「人设」里直接编辑，或把同名文件放进 `<实例目录>/persona/` 覆盖包内默认（`src/hakimi/persona/`）。每个文件的用途见 [人设说明](src/hakimi/persona/README.md)；视频模块的人设在 `bili_companion/personas/`。
- **主人与群内词表**：`<实例目录>/owner.json`，由设置面板「主人与人设」生成，也可以手动补充（下面的注释只是说明，实际文件里不能写注释）：

  ```jsonc
  {
    "name": "主人",                 // 机器人怎么称呼你
    "short_name": "主人",           // 口语里的简称
    "aliases": ["你的群名片"],       // 其它能认出你的昵称
    "ids": ["<官方通道成员 ID>"],    // 你的账号，拥有管理员权限
    "pet_session": "private:<ID>",  // 桌宠写入记忆时挂在哪个私聊会话下
    "known_bot_ids": [],            // 群里的其它机器人，不当作人看待
    "local": {
      "privacy_phrases": [],        // 出现这些短语时，桌宠观察按私聊处理、不进共享记忆
      "public_group_keywords": [],  // 窗口标题含这些词时视为公开群聊
      "signature_terms": [],        // 群梗、外号：作为核心记忆保留更久
      "no_instant_wake_ids": []     // 刷屏账号：被他们 @ 时不立即唤醒
    }
  }
  ```

改完后重启相关服务生效（`systemctl --user restart 'hakimi-*'`）。

## 常用命令

```bash
hakimi modules                 # 已安装 / 已启用的模块（受 aux_config.json 与模块开关共同控制）
hakimi arch                    # 查看七层代码架构分布与模块规模
hakimi arch --check            # 静态检查导入方向（防止下层逆向依赖上层）
hakimi setup status            # 已配置的项目（不显示密钥）
hakimi gateway status          # 模型网关：上游、模型映射、本月请求数与估算花费
hakimi gateway models          # 列出上游 Key 可用的模型
hakimi config check            # 检查实例配置文件（含 aux_config.json 语义校验）
hakimi features                # 功能清单与开关状态
systemctl --user status 'hakimi-*'
journalctl --user -u hakimi-agent -f   # 看回复服务的日志
```

## 更新

```bash
cd hakimi && git pull
.venv/bin/pip install -e ".[qq,video]"        # 按你装的模块写；依赖没变时可省略
systemctl --user restart 'hakimi-*.service'
```

实例目录里的数据和配置不受更新影响。

## 数据与隐私

- **密钥只保存在你的服务器上**：主模型 Key 保存在网关目录（权限 0600），各模块进程拿不到；QQ AppSecret 与设置面板填入的各路独立辅助 API Key（`credentials.fernet` 中的 `provider` 与 `aux` 段）均用 Fernet 加密保存在 `~/.config/hakimi/`；B 站 Cookie 加密保存在视频模块的私有目录。面板只显示“已保存/未填写”，不回显任何值。
- **聊天记录、记忆库、日记都在实例目录里**，不在代码仓库中；`.gitignore` 已排除这些目录，请不要把实例目录提交到 git。
- **隐私边界**：记忆按群隔离，群聊里不会引用私聊内容；桌宠看到你在和别人私聊时只陪你吐槽，既不写进共享记忆也不写入交接摘要；跨模块只交换经过脱敏校验的摘要与自传，绝不复制他人私聊原文与真实账号信息。
- **面板安全**：只监听 `127.0.0.1`，需要令牌，写操作校验同源。

## 开发与测试

```bash
python -m hakimi arch --check                    # 校验七层模块依赖方向
python tests/run_tests.py                        # 主项目测试（tests/known_stale.py 里的用例按预期失败处理）
cd bili_companion && PYTHONPATH=src python -m unittest discover -s tests
```

- 代码结构与分层契约见 [架构说明](docs/architecture.md)；新功能请在 `src/hakimi/features/registry.py` 登记，再用 `hakimi features --markdown` 更新功能清单。
- 测试只使用脱敏数据和假身份（`tests/fixtures/`），不会发送任何真实消息或模型请求。
- 目前主项目测试里有少量已知失败用例；提交改动时请确认没有新增失败与越层依赖。
- 欢迎提 Issue 和 Pull Request。

## 文档

- [模块说明](docs/modules.md)：每个模块装什么、启动哪些服务、互相怎么共享记忆
- [架构说明](docs/architecture.md)：七层架构、导入方向与核心目录映射
- [模型网关](docs/gateway.md)：模型映射、单价与月度上限、代理、兼容地址
- [配置说明](docs/configuration.md) · [功能一览](docs/features.md) · [联网搜索路由](docs/search-routing.md)
- [记忆与自传联动](docs/memory_operation.md) · [日记](docs/diary_operation.md) · [数学排版](docs/math_tool.md) · [B 站视频观察](docs/bilibili_video_operation.md) · [备用模型](docs/fallback_operation.md) · [语音](docs/voice.md)
- [人设与提示词](src/hakimi/persona/README.md)：改名字、改性格、加风格样本
- 视频模块：[bili_companion/README.md](bili_companion/README.md)

## 许可

[MIT](LICENSE)。

本项目与腾讯、哔哩哔哩、Google 均无关联。使用 QQ 机器人和 B 站账号时请遵守各平台的规则；自动发弹幕、评论等功能默认关闭，开启前请确认符合平台规定。
