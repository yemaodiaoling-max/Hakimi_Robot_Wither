# 功能一览

由 `hakimi features --markdown` 生成；登记表在 `src/hakimi/features/registry.py`。

## QQ 官方机器人通道（`official_lane`）

收发群聊/私聊消息、图片与文件，管理回复窗口和群路由。

- 模块：`hakimi.transport.official_transport`、`hakimi.transport.official_routes`、`hakimi.transport.official_api`、`hakimi.transport.official_lane`、`hakimi.transport.official_files`、`hakimi.chat.official_groups`
- 模型可调用的工具：`members.mention`
- 开关：service `hakimi-worker`；config `runtime:official_lane_config.json:allow_private_chat`（私聊）

## 对话调度（`scheduling`）

决定何时开口：被@即时唤醒、上下文积累、相关续聊、每轮回复后的45秒评分窗口、活跃群退让。

- 模块：`hakimi.bridge.conversation_scheduler`、`hakimi.bridge.bridge_daemon`、`hakimi.bridge.participation_policy`、`hakimi.bridge.turn_coordinator`
- 模型可调用的工具：`reply.send`、`reply.skip`、`context.read`、`turn.progress`、`turn.failure`
- 开关：config `runtime:config.json:conversation_scheduler.enabled`；config `runtime:config.json:participation_policy.enabled`（主动参与）

## 按用户控制回复成本（`user_reply_quota`）

同一用户滚动30分钟最多7轮，Gemini与DSH共用，分条回复只计一轮。

- 模块：`hakimi.chat.user_reply_quota`、`hakimi.bridge.turn_coordinator`、`hakimi.bridge.bridge_daemon`
- 备注：发布任务前检查，提交回复时按实际回复对象复查；历史随turn_state持久化，跨群及私聊共用同一user_id额度。

## 冷场主动抛话题（`cold_topics`）

群里长时间安静时，按配置窗口主动开启话题。

- 模块：`hakimi.bridge.cold_topics`
- 开关：config `runtime:config.json:lurker_schedule.proactive_topic.enable`

## Gemini 主回复（`gemini`）

经私有 Vertex 网关直连 Gemini，按群保留会话，月度用量由管理员手动控制。

- 模块：`hakimi.models.vertex_route`、`hakimi.models.vertex_session`、`hakimi.models.scoped_session`、`hakimi.models.provider_health`、`hakimi.ops.cloud_service`
- 模型可调用的工具：`web.search`、`web.read`、`web.sources`、`time.now`、`calculator.evaluate`、`tools.list`、`runtime.status`
- 开关：service `hakimi-agent`；config `root:state/vertex_route.json:enabled`（Vertex 直连）

## 备用模型兜底（`dsh_fallback`）

主模型超时或不可用时由独立 HTTP API 或 DSH 接管，默认关闭。

- 模块：`hakimi.models.fallback_supervisor`、`hakimi.models.fallback_http`、`hakimi.models.fallback_runner`、`hakimi.models.fallback_prompt`、`hakimi.models.fallback_commit`、`hakimi.models.dsh_tools`、`hakimi.models.aux_config`
- 模型可调用的工具：`image.read`、`bots.propose`
- 开关：config `runtime:config.json:fallback.enabled`；config `root:aux_config.json:fallback.enabled`（辅助总配置开关）

## QQ 主模型热切换通道（`qq_backup`）

主回复达到额度或故障时，切换到独立的兼容 API 或伴侣通道，默认关闭。

- 模块：`hakimi.models.video_hydrogen`、`hakimi.models.hydrogen_duty`、`hakimi.models.gateway_text`
- 开关：config `root:aux_config.json:qq_backup.enabled`

## QQ语音（`voice`）

本地CPU转写、会话私有原文与明确请求时的原创音色/短哼唱。

- 模块：`hakimi.memory.voice_receipts`、`hakimi.voice.worker`、`hakimi.voice.delivery`、`hakimi.voice.tts`
- 模型可调用的工具：`voice.read`、`voice.speak`、`voice.hum`
- 开关：service `hakimi-voice`；config `root:voice_config.json:enabled`；config `root:voice_config.json:tts_enabled`（明确请求发声）

## 长期记忆（`memory`）

原文存储、全文+语义混合检索、按群范围隔离，笔记原文不可改写。

- 模块：`hakimi.memory.memory_store`、`hakimi.memory.store_write`、`hakimi.memory.store_retrieval`、`hakimi.memory.smart_memory`、`hakimi.memory.memory_lifecycle`、`hakimi.tools.memory_result`
- 模型可调用的工具：`memory.search`、`memory.read`、`memory.remember`
- 开关：service `hakimi-memory-embedding`（语义向量服务）

## 日常事件卡（`daily_events`）

在既有人物笔记批次中整理少量真实事件，支持复用主网关或独立复盘 API。

- 模块：`hakimi.memory.daily_events`、`hakimi.curation.impression_events`、`hakimi.memory.relationship_events`、`hakimi.curation.post_reply_review`、`hakimi.curation.review_runtime`、`hakimi.memory.review_audit`、`hakimi.memory.relationship_history`、`hakimi.memory.store_maintenance`
- 开关：config `root:aux_config.json:review.enabled`
- 备注：真实回复窗口结束后异步复盘，可通过 aux_config.json 配置走主网关、独立 HTTP API、伴侣通道或关闭；保留有界短审核理由与来源，区分事实、关系与原话角色。

## 关系网（`social`）

人物小本子、好感/亲近评分（来源分开记录）、互动熟悉度、记忆图谱。

- 模块：`hakimi.social.memory_social`、`hakimi.social.social_record`、`hakimi.social.social_query`、`hakimi.social.social_behavior`、`hakimi.social.relation_perspective`、`hakimi.memory.memory_graph`
- 模型可调用的工具：`memory.social`、`memory.social_append`、`memory.graph`
- 开关：config `runtime:social_behavior_policy.json:protected_user_ids`（受保护成员）

## 记忆与关系网面板（`console`）

本机网页面板（SSH 隧道访问，令牌保护）：关系图、人物、记忆、身份关联、预算、复盘变化。

- 模块：`hakimi.console.memory_console`、`hakimi.console.payloads_social`、`hakimi.console.payloads_memory`、`hakimi.console.payloads_identity`、`hakimi.console.payloads_review_history`
- 开关：service `hakimi-memory-console`
- 备注：端口 18768

## 新旧身份关联（`identity`）

把旧 QQ 号记忆和官方通道成员人工关联，按关联读取旧笔记。

- 模块：`hakimi.social.identity_links`、`hakimi.social.identity_edit`、`hakimi.social.identity_legacy`

## 内部复盘（`personal_review`）

每两小时挑选最多三位成员整理印象与记忆（不发群消息）。

- 模块：`hakimi.curation.personal_review`、`hakimi.memory.review_select`

## QQ 绘图（`drawing`）

收到绘图请求时由哈基米构思画面，使用独立的 Gemini 绘图站点生成并回传图片。

- 模块：`hakimi.tools.drawing`、`hakimi.tools.drawing_intent`、`hakimi.tools.drawing_delivery`、`hakimi.tools.drawing_accounting`
- 模型可调用的工具：`image.draw`
- 开关：config `runtime:state/drawing_provider.json:enabled`（启用 QQ 绘图）

## 数学排版（`math`）

Markdown/TeX 公式本地排成图片发送。

- 模块：`hakimi.tools.math_render`、`hakimi.models.dsh_math`
- 模型可调用的工具：`math.send`

## 文件与聊天导出（`files`）

发送本地文件、导出聊天记录。

- 模块：`hakimi.chat.file_delivery`、`hakimi.transport.official_files`
- 模型可调用的工具：`files.list`、`files.send`、`chat.export`

## 表情包（`stickers`）

按语境从图库选图、收藏群友表情（屏蔽确认过的奶龙）。

- 模块：`hakimi.tools.sticker_policy`、`hakimi.tools.sticker_catalog`
- 模型可调用的工具：`stickers.list`、`stickers.collect`
- 开关：config `runtime:config.json:sticker_policy.enabled`

## 图片与视频理解（`vision`）

读取本轮授权的图片；按需抽帧观看 B 站与 QQ 小程序视频。

- 模块：`hakimi.chat.image_permissions`、`hakimi.tools.bilibili_video`、`hakimi.models.fallback_media`
- 模型可调用的工具：`images.list`、`video.watch`、`video.watch_attachment`、`video.watch_miniapp`

## 私有日记（`diary`）

每晚 22:30（纽约时间）写一篇日记并由 Terra 配图，可导出 PDF。

- 模块：`hakimi.features.diary_daily`、`hakimi.features.diary_store`、`hakimi.features.diary_latex`
- 模型可调用的工具：`diary.read`、`diary.export`
- 开关：timer `hakimi-diary.timer`

## QQ 空间说说（`qzone`）

旧版每日说说（已由私有日记取代，云端未迁移）。

- 模块：`hakimi.features.qzone_publisher`、`hakimi.features.qzone_fallback`
- 开关：absent `root:state/diary_mode.json`（未被日记取代）

## 投喂（`feeding`）

主人绑定后用 /投喂 记录虚拟投喂，两种模型都能读到回执。

- 模块：`hakimi.tools.feeding`

## 专家求助队列（`codex_help`）

复杂问题或故障分析时排队请独立模型或 Codex CLI 给内部分析，默认关闭。

- 模块：`hakimi.features.codex_help`、`hakimi.features.codex_help_runner`、`hakimi.features.codex_help_service`
- 开关：config `root:codex_help_config.json:enabled`；config `root:aux_config.json:ops.codex_help_enabled`（总配置开关）；service `hakimi-help`

## 健康巡查 / Terra 督导 / Sol 修复（`supervision`）

按独立开关执行服务看门狗、Terra 两小时评价及 Sol 最小修复，默认全部关闭。

- 模块：`hakimi.ops.cloud_supervisor`、`hakimi.ops.terra_supervision`、`hakimi.ops.terra_budget`、`hakimi.ops.sol_repair`
- 开关：config `root:aux_config.json:ops.watchdog_enabled`（确定性看门狗）；config `root:aux_config.json:ops.terra_supervision_enabled`（Terra督导）；config `root:aux_config.json:ops.sol_repair_enabled`（Sol修复）；timer `hakimi-terra-watch.timer`

## 模型试用（`model_trial`）

24 小时限时试用另一个 Gemini 型号，由 Terra 对比评价后自动恢复。

- 模块：`hakimi.models.model_trial`

