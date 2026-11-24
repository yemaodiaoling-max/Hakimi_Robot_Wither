# 哈基米 Hakimi

一只住在 QQ 群、B 站和你电脑桌面上的猫系 AI 同伴。三个形态共用**同一个记忆内核**：在 QQ 群里聊过的事、在 B 站看过的视频、桌宠陪你写代码的经历，都会成为同一只哈基米的记忆。

| 模块 | 做什么 | 运行在 |
|---|---|---|
| **记忆内核**（必装） | 长期记忆（全文 + 可选语义检索）、关系网与好感度、日记、信息面板、设置面板、本地模型网关 | Linux 服务器或 Windows |
| **QQ 机器人** `qq` | QQ 官方机器人通道：群聊/私聊、被 @ 即答、冷场主动聊、表情包、公式排版成图、看图看视频、每日日记 | 同上 |
| **视频机器人** `video` | 自己刷 B 站推荐视频、盯指定直播间、可选发弹幕/评论，看过的内容写进共享记忆 | Linux 服务器（需要 ffmpeg） |
| **桌宠** `pet` | Windows 桌面宠物：看屏幕吐槽、语音对话、帮你操作电脑，经历同步进记忆 | Windows 电脑 |

三个模块可以任意组合：只装一个、两个或全部都可以，没启用的模块不会启动，也不会影响其它模块。

## 快速开始（Linux 服务器）

需要 Python 3.12+、git；视频模块另需 `ffmpeg`。

```bash
git clone <本仓库地址> hakimi && cd hakimi
deploy/install.sh --modules qq,video,pet      # 只要 QQ 机器人就写 --modules qq
```

安装脚本会创建虚拟环境、实例目录（默认 `~/hakimi-data`）、本地模型网关证书和面板令牌，并以 systemd 用户服务启动**网关**和**面板**。然后打开设置面板：

```bash
# 在你自己的电脑上建立隧道（面板只监听服务器本机）
ssh -L 18768:127.0.0.1:18768 用户@服务器
# 在服务器上打印带令牌的地址
HAKIMI_ROOT=~/hakimi-data .venv/bin/hakimi setup url
```

在浏览器打开打印出的 `http://127.0.0.1:18768/setup.html#token=...`，依次填写：

1. **模块**：勾选要用的模块。
2. **模型与 Key**：填 [Google AI Studio](https://aistudio.google.com/apikey) 的 Gemini API Key（或任何 Gemini 协议兼容地址），点「检测」读取可用模型，选择主回复/轻量/视频模型。可设每月花费上限。
3. **主人与人设**：机器人名字、你的称呼和 ID。
4. **QQ 机器人**：在 [QQ 开放平台](https://q.qq.com) 创建机器人，填 AppID、AppSecret、测试群号。
5. **B 站**：扫码登录（或手动粘贴 Cookie），可选优先直播间。
6. **桌宠**：选择本机或 SSH 连接，复制生成的 `pet_connection.json`。

勾选「保存后重启相关服务」并保存，面板会把设置写进各模块的配置文件并启动对应服务。

## 桌宠（Windows）

```powershell
git clone <本仓库地址> hakimi; cd hakimi
python -m venv .venv; .venv\Scripts\pip install -e .[pet]
# 把设置面板「桌宠」一节生成的内容保存为 desktop_pet\pet_connection.json
.venv\Scripts\python desktop_pet\pet_app.py
```

机器人装在同一台 Windows 电脑上时选“本机”，桌宠会直接在本机启动后端；装在服务器上时选“SSH”，桌宠通过 SSH 连接服务器。

## 常用命令

```bash
hakimi modules                 # 已安装/已启用的模块
hakimi setup status            # 已配置项（不显示密钥）
hakimi gateway status          # 模型网关：上游、模型映射、本月估算花费
hakimi config check            # 检查实例配置
python tests/run_tests.py      # 测试
systemctl --user status 'hakimi-*'
```

## 数据与隐私

- 所有密钥只保存在你的服务器上：模型 Key 在网关目录（0600），QQ AppSecret / DeepSeek Key 用 Fernet 加密在 `~/.config/hakimi/`，B 站 Cookie 加密在视频模块的私有目录。面板只显示“已保存/未填写”，不回显任何值。
- 聊天记录、记忆库、日记都在实例目录（默认 `~/hakimi-data`）里，不在代码仓库中；`.gitignore` 已排除这些目录，请不要把实例目录提交到 git。
- 面板只监听 `127.0.0.1`，需要令牌，写操作校验同源。

## 文档

- [模块说明](docs/modules.md)：每个模块装什么、启动哪些服务、互相怎么共享记忆
- [模型网关](docs/gateway.md)：模型映射、单价与月度上限、代理、兼容地址
- [架构说明](docs/architecture.md) · [配置说明](docs/configuration.md) · [功能一览](docs/features.md)
- [人设与提示词](src/hakimi/persona/README.md)：改名字、改性格、加风格样本
- 视频模块：[bili_companion/README.md](bili_companion/README.md)

## 许可

[MIT](LICENSE)。本项目与腾讯、哔哩哔哩、Google 无关；使用 QQ 机器人与 B 站账号时请遵守各平台的规则。
