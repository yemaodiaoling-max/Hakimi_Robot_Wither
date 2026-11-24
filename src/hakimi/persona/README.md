# 人设与提示词

哈基米的人设、风格和各个模型角色的提示词都是这个目录里的文本文件，代码只按文件名读取、逐字使用。

## 怎么改人设（推荐）

打开设置面板 → **人设**，选一个文件直接编辑并保存。保存的内容写到实例目录的 `persona/` 下，**覆盖**这里的默认文件；「恢复默认」会删掉你的版本。升级代码不会冲掉你的修改。改完重启对应服务（面板会提示）。

也可以手动放文件：`<实例>/persona/<文件名>` 同名即覆盖；目录可用环境变量 `HAKIMI_PERSONA_DIR` 改。

| 文件 | 面板名称 | 用在哪里 |
|---|---|---|
| `gemini_persona.txt` | QQ · 核心人设 | 每轮主回复都带的精简人设：性格、分条长度、工具使用、话题规避 |
| `style_guide.md` | QQ · 完整风格指南 | 完整人设与表达风格；`## 工具与数据边界` 之前的部分也用于内部复盘和日记 |
| `gemini_core_identity.txt` | QQ · 一句话身份 | 每轮上下文的身份声明 |
| `owner_guidance.txt` | QQ · 对主人的语气 | 主人发言时追加的语气指引（主人在面板「主人」里设置） |
| `owner_privilege.txt` | QQ · 主人身份说明 | 主人发言时上下文里的身份说明 |
| `dsh_core_identity.txt` | 备用模型 · 一句话身份 | DeepSeek 备用模型接管时 |
| `dsh_runtime_prefix.txt` | 备用模型 · 人设前缀 | DeepSeek 的人设前缀和 JSON 回复协议（协议部分请保留） |
| `diary_writer.txt` | 日记 · 写作口吻 | 每晚写日记的角色与格式 |
| `desktop_pet_persona.txt` | 桌宠 · 人设 | 桌宠形态的人设与隐私规则 |
| `bili_companion/personas/hakimi_core.md` | 视频 · 核心人设 | B 站形态（自定义版本存在 `<实例>/persona/video/`） |
| `bili_companion/personas/bilibili_overlay.md` | 视频 · B 站行为 | 看视频、弹幕、评论的平台规则 |

改名字：机器人在群里显示的名字由 QQ 开放平台决定；人设里的自称在上面这些文件里，面板「主人」一节的“机器人名字”会写进 `config.json`。

所有文本按原样逐字使用（不去首尾空白、不做格式化），改的时候注意保留结尾换行与否。

## 风格样本

| 文件 | 说明 |
|---|---|
| `style_examples.jsonl` | 对话风格样本，按群风格挑选作为示例；默认为空。每行一个 JSON：`{"id","provenance":"real_human_dialogue","profile","tags","context":[{"speaker","text"}],"decision":"reply","reply","source":{...}}` |
| `dsh_style_examples.jsonl` | 备用模型的风格样本；默认为空，接入新群时会在运行目录追加 |

只放你有权使用的对话（例如自己写的、或得到当事人同意的）。

## 运维角色（不面向群友）

| 文件 | 角色 |
|---|---|
| `ops/terra_supervisor.txt` | Terra 督导：两小时一次的风格与关系评价 |
| `ops/terra_bot_reviews.txt` | Terra 督导附加段：确认疑似机器人账号 |
| `ops/sol_repair.txt` | Sol 运维修复员：根据故障指标提出最小代码修复 |
| `ops/codex_analyst.txt` | Codex 内部分析助手：故障分析与难题推导，只分析不发言 |

## 不在这里的

- `state/gemini_style_feedback.md`、`state/dsh_style_feedback.md`：Terra 巡查写出的**动态**风格建议，属于运行数据。
- 群风格配置（每个群用哪种语气、长度上限）：`group_profiles.json`，见 `docs/configuration.md`。
