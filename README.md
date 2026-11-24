# 哈基米 Hakimi

**简体中文** · [English](README.en.md)

一只住在 QQ 群、B 站和你电脑桌面上的猫系 AI 同伴。三种形态共用**同一个记忆内核**：在 QQ 群里聊过的事、在 B 站看过的视频、桌宠陪你写代码的经历，都会成为同一只哈基米的记忆，它会在下一次聊天时自然地想起来。

- 🐱 **会挑时机说话的群友**：被 @ 立即回应，其它时候先读上下文再决定要不要接话；群里冷场时会主动抛话题，热闹时懂得退让。
- 🧠 **长期记忆与关系网**：记得每个群友说过的话、外号和梗，给每个人记“小本子”，好感与亲近度随互动变化；每晚写一篇私有日记。
- 📺 **自己刷 B 站**：按兴趣看推荐视频、蹲守指定直播间，看过的内容会变成和 QQ 群共享的谈资。
- 🖥️ **桌面宠物**：趴在 Windows 桌面上看屏幕吐槽、语音聊天、在你确认后帮你操作电脑。
- 🔐 **密钥只在你的服务器上**：所有模型请求都经过本机网关，统一记账、可设月度花费上限。

## 目录

- [模块](#模块) · [架构](#架构) · [功能一览](#功能一览)
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
                 ┌──────────── 记忆内核 core ────────────┐
                 │ 记忆库 memory.sqlite3 · 关系网 · 日记  │
  QQ 机器人 ───▶ │ 信息面板 / 设置面板 (:18768)           │ ◀─── 桌宠（Windows，本机或 SSH）
                 │ 本地模型网关 (:18765) ──▶ Gemini API   │
                 └───────────────▲───────────────────────┘
                                 │ 每 30 分钟交换摘要
                           视频机器人（B 站）
```

- 代码是一个可安装的 Python 包 `hakimi`（`src/hakimi/`），视频模块是同仓库下的 `bili_companion/` 包。
- 运行数据（配置、记忆库、日记、发送记录）放在**实例目录**里（默认 `~/hakimi-data`），和代码完全分开。
- 所有模型调用都只发到本机网关 `https://127.0.0.1:18765`，由网关转发到你配置的上游（Google AI Studio 或任何 Gemini 协议兼容地址）。

## 功能一览

**对话**
- 被 @ 或私聊即时回应；平时积累上下文、给每轮发言打分后再决定是否插话，活跃群里主动退让。
- 冷场时按时间窗口主动开启话题。
- 按用户限流：同一个人 30 分钟内最多触发 7 轮回复，防刷屏、控成本。
- 主模型超时或不可用时，可由 DeepSeek 备用模型用同一套工具接管。

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
- 设置面板：模块开关、模型与 Key、主人与人设、QQ 机器人、B 站登录、桌宠连接，保存后自动写配置并重启相关服务。

**运维**
- 本地模型网关：统一记账，按单价估算花费，可设月度上限；达到上限后自动切备用模型或暂停看视频。
- 可选健康巡查与自动修复（需要安装 `codex` 命令行）。

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
2. **模型与 Key**：填 [Google AI Studio](https://aistudio.google.com/apikey) 的 Gemini API Key（或任何 Gemini 协议兼容地址），点「检测」读取可用模型，选择主回复、轻量、视频模型，可设每月花费上限。
3. **主人与人设**：机器人名字、你的称呼和 ID。
4. **QQ 机器人**：在 [QQ 开放平台](https://q.qq.com) 创建机器人，填 AppID、AppSecret 和测试群号。
5. **B 站**：扫码登录（或手动粘贴 Cookie），可选一个优先直播间。
6. **桌宠**：选择本机或 SSH 连接，复制生成的 `pet_connection.json`。

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
hakimi modules                 # 已安装 / 已启用的模块
hakimi setup status            # 已配置的项目（不显示密钥）
hakimi gateway status          # 模型网关：上游、模型映射、本月请求数与估算花费
hakimi gateway models          # 列出上游 Key 可用的模型
hakimi config check            # 检查实例配置文件
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

- **密钥只保存在你的服务器上**：模型 Key 在网关目录（权限 0600），各模块进程拿不到；QQ AppSecret、DeepSeek Key 用 Fernet 加密保存在 `~/.config/hakimi/`；B 站 Cookie 加密保存在视频模块的私有目录。面板只显示“已保存/未填写”，不回显任何值。
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
