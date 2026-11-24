# 配置说明

日常使用只需要**设置面板**（`/setup.html`）：它把你填的内容写进下面这些文件。想手动调整行为（回复节奏、主动发言频率等）时再看本页。

```bash
hakimi config list     # 所有配置文件及用途
hakimi config check    # 检查格式、必需字段、群号/档案引用等
hakimi config show config.json --where runtime
```

`root` 指实例目录（`HAKIMI_ROOT`，安装脚本默认 `~/hakimi-data`），`runtime` 指其中的 `official_test_runtime/`（QQ 通道的运行目录，名字是历史遗留）。

## 由设置面板管理的文件

| 文件 | 位置 | 内容 |
|---|---|---|
| `hakimi_settings.json` | root | 面板里的全部非密钥设置 |
| `modules.json` | root | 启用的模块 |
| `owner.json` | root | 主人称呼、ID、其它机器人账号 |
| `official_test_config.json` | root | QQ 官方通道：AppID、测试群、模式、私聊/主动发言 |
| `task_routing.json` | root | 单模型或智能分流 |
| `state/vertex_route.json` | root | 主回复走本地网关（`enabled`、网关目录） |
| `gateway.json`、`upstream.key` | 网关目录 | 上游地址、模型映射、单价、月度上限、API Key |
| `~/.config/hakimi/credentials.fernet` | 用户目录 | QQ AppSecret、DeepSeek Key（加密） |
| `bili_companion/deploy/watcher.json`、`bridge.json` | 视频模块 | 观看配置、记忆桥路径 |

面板保存时会调用 `official_test.prepare`，从 root 的配置生成 runtime 里的 `config.json`、`group_profiles.json`、`official_lane_config.json`。手动改了 root 的配置后，运行 `hakimi setup apply` 重新生成。

## 手动调整：`config.json` 各段

| 段 | 内容 |
|---|---|
| `bot_profile` | 名字、活跃/忽略/低频群、忽略的用户、是否接私聊 |
| `lurker_schedule` | 潜水窗口：随机查看间隔、被@/私聊即时唤醒、各群冷却；`proactive_topic` 是冷场主动抛话题 |
| `group_weights` | 每个群的参与权重与最低分 |
| `topic_bias` | 偏好关键词、忽略关键词 |
| `delivery_policy` | 发送节奏：全局/单群间隔、每分钟上限、去重窗口、平台报错暂停、分条上限 |
| `participation_policy` | 主动参与：开关、单群/全局 24 小时次数 |
| `fallback` | 备用模型：开关、主模型宽限与超时、调用间隔与每小时上限 |
| `sticker_policy` | 表情包开关（图库放在 `root/stickers/`） |
| `conversation_scheduler` | 统一调度开关、繁忙群敏感度、@后的合并等待秒数 |
| `always_ignored_keywords` | 一律不接的话题词 |
| `group_reply_overrides` | 按群的主动参与概率，以及可选的回复字数、条数上限 |

`group_profiles.json` 为每个群指定语气档案（`quiet_default`、`tech_banter`、`gentle_daily` 等，见文件内的说明），新群接入时自动追加。

## 人设

人设与全部提示词在 `src/hakimi/persona/`（见该目录的 README）。在 runtime 目录放一个同名文件即可覆盖包内版本，例如 `root/official_test_runtime/gemini_persona.txt`（重启 `hakimi-agent` 生效）。风格样本 `style_examples.jsonl` / `dsh_style_examples.jsonl` 默认为空：放进 root 后，`hakimi setup apply` 会把它们复制到 runtime；只加入你自己有权使用的对话样本。
