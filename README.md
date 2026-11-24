# 哈基米 Hakimi

**简体中文** · [English](README.en.md)

一只住在 QQ 群、B 站和你电脑桌面上的猫系 AI 同伴。三种形态共用**同一个记忆内核**：在 QQ 群里聊过的事、在 B 站看过的视频、桌宠陪你写代码的经历，都会成为同一只哈基米的记忆，它会在下一次聊天时自然地想起来。

- ⚡ **单 API 开箱即用，也可按需拆分多 API / 多 Agent**：开源版只需填写**一个 API Key**（支持 Google Gemini、Gemini 兼容或任意 OpenAI 兼容接口）即可跑通完整功能；备用模型、异步复盘、上下文筛选、联网预搜索与运维督导均支持在设置面板独立配置，默认全部关闭或复用主网关。
- 🐱 **会挑时机说话的群友**：被 @ 立即回应，其它时候先读上下文再决定要不要接话；群里冷场时会主动抛话题，热闹时懂得退让。
- 🧠 **长期记忆与关系网**：记得每个群友说过的话、外号和梗，给每个人记“小本子”，好感与亲近度随互动变化；每晚写一篇私有日记。
- 📺 **自己刷 B 站**：按兴趣看推荐视频、蹲守指定直播间，看过的内容会变成和 QQ 群共享的谈资。
- 🖥️ **桌面宠物**：趴在 Windows 桌面上看屏幕吐槽、语音聊天、在你确认后帮你操作电脑。
- 🔐 **密钥只在你的服务器上**：所有模型请求都经过本机网关，统一记账、可设月度花费上限；辅助 API 密钥单独加密保存。

## 目录

- [模块](#模块) · [架构](#架构) · [单 API 开箱与独立模块开关](#单-api-开箱与独立模块开关) · [功能一览](#功能一览)
- [快速开始（Linux 服务器）](#快速开始linux-服务器) · [桌宠（Windows）](#桌宠windows)
- [自定义人设与身份](#自定义人设与身份) · [常用命令](#常用命令) · [更新](#更新)
- [数据与隐私](#数据与隐私) · [开发与测试](#开发与测试) · [文档](#文档) · [许可](#许可)

## 模块

由一个必装的**记忆内核**和三个可选模块组成，可以任意组合：只装一个、两个或全部都行。没启用的模块不会启动，也不会影响其它模块。

| 模块 | 做什么 | 运行在 |
|---|---|---|
| **记忆内核** `core`（必装） | 长期记忆（全文检索，可选语义检索）、关系网与好感度、日记、信息面板、设置面板、本地模型网关 | Linux 服务器（也可在 Windows 上运行） |
| **QQ 机器人** `qq` | 走 QQ 官方机器人通道：群聊/私聊、被 @ 即答、冷场主动聊天、表情包、公式排版成图、看图看视频、可选 AI 绘图、每日日记 | 同上 |
| **视频机器人** `video` | 自己刷 B 站推荐、盯指定直播间、可选发弹幕和评论，看过的内容写进共享记忆 | Linux 服务器（需要 `ffmpeg`） |
| **桌宠** `pet` | Windows 桌面宠物：看屏幕吐槽、语音对话、帮你操作电脑，经历同步进记忆 | Windows 电脑 |

## 架构

```
                 ┌─────────────── 记忆内核 core ───────────────┐
                 │ 记忆库 memory.sqlite3 · 关系网 · 日记       │
  QQ 机器人 ───▶ │ 信息面板 / 设置面板 (:18768)                │ ◀─── 桌宠（Windows，本机或 SSH）
                 │ 本地模型网关 (:18765) ──▶ Gemini / OpenAI   │
                 └───────────────▲───────────────────────┬─────┘
                                 │ 每 30 分钟交换摘要     │ 可选辅助扩展（aux_config.json，默认关/复用网关）
                           视频机器人（B 站）            └─▶ 独立复盘/备用 HTTP API · 可选 CLI Agent
```

- **同一套代码，纯配置区分**：下载开源版直接运行，与开启多路高可用、多 Agent 协作的生产实例使用的是同一套核心代码；所有行为差异和独立端点都在**实例目录**（默认 `~/hakimi-data`）中通过配置文件开启。
- **核心与视频包结构**：主程序是可安装的 Python 包 `hakimi`（`src/hakimi/`），视频模块是同仓库下的 `bili_companion/` 包。
- **开箱只需一个上游 API**：所有核心调用经本机网关 `https://127.0.0.1:18765` 转发，支持 Google Gemini、Gemini 兼容端点或任何 OpenAI Chat Completions 兼容接口（如 DeepSeek、OpenRouter、自建网关等）。

## 单 API 开箱与独立模块开关

为了既能“填一个 Key 就能用”，又兼容生产环境给不同职责挂不同模型与外部 Agent，所有辅助能力统一由实例目录下的 `aux_config.json` 与 `/setup.html` 面板的 **「高级与独立 API」** 管理：

| 模块 / 辅助能力 | `aux_config.json` 键 | 开源默认（开箱） | 可选独立模式 |
|---|---|---|---|
| **异步复盘与日常事件卡** | `review` | `mode: "main_gateway"`（复用主网关） | `custom_api`（独立 OpenAI/Gemini HTTP API）、`companion_duty` 或关闭 |
| **QQ 主模型热切换通道** | `qq_backup` | `enabled: false`（`mode: "disabled"`） | `custom_api`（独立 OpenAI/Gemini HTTP API，支持 `fallback_model`）或 `companion_duty` |
| **主模型故障兜底** | `fallback` | `enabled: false`（`backend: "http_api"`） | 开启后走独立 HTTP API（无需外部 CLI）或外部 `dsh` CLI |
| **上下文与续聊筛选裁判** | `context_judge` | `mode: "heuristic"`（本地启发式，零调用） | `main_gateway`、`custom_api` 或 `dsh` |
| **联网预搜索与付费搜索** | `search_routing` | `enabled: false`，`judge_mode: "local_rules"`，`paid_search_mode: "disabled"` | 可启用预搜索判断（`main_gateway` / `custom_api` / `dsh`）与 DeepSeek 付费搜索；关闭时手动 `web.search` 仍可用免费源 |
| **每日日记兜底写手** | `diary.fallback_enabled` | `false`（仅主模型，不依赖外部兜底） | `true`（主模型不可用时调用已启用的 `fallback` 通道） |
| **运维看门狗 / Terra 督导 / Sol 修复 / 专家求助** | `ops.*` | 4 项开关全部 `false`（零额外后台督导进程） | 逐项独立开关（`watchdog_enabled`、`terra_supervision_enabled`、`sol_repair_enabled`、`codex_help_enabled`），可选 `main_gateway`、`custom_api` 或 `codex_cli` |

> 普通用户保持默认配置即可运行全部聊天、记忆、日记、搜图、公式排版等核心体验；如需给某一项挂单独的低价模型或外部 CLI，只需在设置面板展开 **「高级与独立 API」** 填写并保存，配置格式可通过 `hakimi config check` 校验。详细配置键见 [docs/configuration.md](docs/configuration.md)。

## 功能一览

**对话**
- 被 @ 或私聊即时回应；平时积累上下文、给每轮发言打分后再决定是否插话，活跃群里主动退让。
- 冷场时按时间窗口主动开启话题。
- 按用户限流：同一个人 30 分钟内最多触发 7 轮回复，防刷屏、控成本。
- 备用通道、独立复盘 API、上下文筛选裁判、联网预搜索与运维督导均**默认关闭或复用主网关**，开箱无需额外 API 或 CLI；需要高可用时按上表按需启用。

**记忆与关系**
- 原文存储，全文 + 可选语义混合检索；按群隔离，群聊里不会说出私聊内容。
- 人物小本子、好感/亲近评分（来源分开记录）、互动熟悉度、记忆图谱。
- 回复结束后异步复盘，整理事件卡和人物印象；每两小时挑几位成员做内部复盘（不发群消息）。
- 每晚写一篇私有日记并配图，可导出 PDF。

**多媒体**
- 看图（只读本轮授权的图片）、按需抽帧观看 B 站与 QQ 小程序视频。
- Markdown/TeX 公式在本地排版成图片发送。
- 按语境从图库挑表情包，收藏群友的表情。
- 可选 AI 绘图：默认关闭，在面板「绘图设置」里填写自己的图像模型地址和 Key，并设置每日/每月花费上限。

**面板**（只监听本机，令牌保护，通过 SSH 隧道访问）
- 关系面板（关系图、人物、跨群的好感与档案）、消息日志、旧群与成员匹配、视频活动、复盘变化、调用预算、绘图设置。
- 设置面板：模块开关、模型与 Key、主人与人设、QQ 机器人、B 站登录、桌宠连接，以及默认折叠的 **「高级与独立 API」**，保存后自动写配置并重启相关服务。

**运维**
- 本地模型网关：统一记账，按单价估算花费，可设月度上限；达到上限后自动切换已配置的备用通道或暂停看视频。
- 可选服务看门狗、Terra 风格评价、Sol 最小修复与专家求助队列（全部默认关闭，可在面板或 `aux_config.json` 单独开关与指定后端）。

完整清单与每项功能的开关见 [docs/features.md](docs/features.md)。

## 快速开始（Linux 服务器）

需要 Python 3.12+ 和 git；视频模块另需 `ffmpeg`。已在 Ubuntu / Debian 上测试。

```bash
git clone https://github.com/yemaodiaoling-max/Hakimi_Robot_Wither.git hakimi && cd hakimi
deploy/install.sh --modules qq,video,pet      # 只要 QQ 机器人就写 --modules qq
# 想要语义检索（约 500 MB）可以加 --vector
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

机器人装在同一台 Windows 电脑上时选“本机”，桌宠会直接在本机启动后端；装在服务器上时选“SSH”，桌宠通过一条常驻的 SSH 连接和服务器通信。桌宠对电脑的每一个操作（打开软件、运行命令、键鼠操作）都会先弹出确认按钮，你点「允许」后才会执行。

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
- **隐私边界**：记忆按群隔离，群聊里不会引用私聊内容；桌宠看到你在和别人私聊时，只陪你吐槽，不写进共享记忆；跨模块只交换整理过的摘要，不复制私聊原文和账号信息。
- **面板安全**：只监听 `127.0.0.1`，需要令牌，写操作校验同源。

## 开发与测试

```bash
python tests/run_tests.py                       # 主项目测试（tests/known_stale.py 里的用例按预期失败处理）
cd bili_companion && PYTHONPATH=src python -m unittest discover -s tests
```

- 代码结构见 [架构说明](docs/architecture.md)；新功能请在 `src/hakimi/features/registry.py` 登记，再用 `hakimi features --markdown` 更新功能清单。
- 测试只使用脱敏数据和假身份（`tests/fixtures/`），不会发送任何真实消息或模型请求。
- 目前主项目测试里有少量已知失败用例；提交改动时请确认没有新增失败。
- 欢迎提 Issue 和 Pull Request。

## 文档

- [模块说明](docs/modules.md)：每个模块装什么、启动哪些服务、互相怎么共享记忆
- [模型网关](docs/gateway.md)：模型映射、单价与月度上限、代理、兼容地址
- [配置说明](docs/configuration.md) · [架构说明](docs/architecture.md) · [功能一览](docs/features.md)
- [记忆](docs/memory_operation.md) · [日记](docs/diary_operation.md) · [数学排版](docs/math_tool.md) · [备用模型](docs/fallback_operation.md) · [语音](docs/voice.md)
- [人设与提示词](src/hakimi/persona/README.md)：改名字、改性格、加风格样本
- 视频模块：[bili_companion/README.md](bili_companion/README.md)

## 许可

[MIT](LICENSE)。

本项目与腾讯、哔哩哔哩、Google 均无关联。使用 QQ 机器人和 B 站账号时请遵守各平台的规则；自动发弹幕、评论等功能默认关闭，开启前请确认符合平台规定。
