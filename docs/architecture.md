# 架构说明

## 从功能找到源码

安装时的 `core / qq / video / pet` 是用户可以启用的产品功能，见[模块说明](modules.md)。开发时按要修改的行为找源码。一个 QQ 回复会经过多个包。

| 想修改什么 | 从哪里入手 | 交给谁处理下一步 |
|---|---|---|
| 收到的 QQ 协议消息、图片或发送回执 | `hakimi.transport`。官方 QQ 和 OneBot 有各自的适配代码 | 收到的事件交给 `hakimi.bridge`；发送回执由对应通道确认 |
| 是否回复、补充消息能否合并 | `hakimi.bridge` 的调度器和轮次协调器 | 需要生成时交给模型会话 |
| 更换模型、备用通道或会话 | `hakimi.models` | 调用模型网关；需要联网证据时调用 `hakimi.web` |
| 搜索网页、管理搜索额度、记录回答引用的来源 | `hakimi.web`。普通网页读取在 `public_pages.py`，来源记录在 `web_sources.py` | 向调用方返回网页内容、来源或失败原因 |
| 修改本地网关额度或账单 | `hakimi.gateway.ledger`。配置和证书在 `config.py`，HTTP 处理在 `server.py` | 客户端与网关共用 `protocol.py` 的项目标记和预算字段 |
| 在设置页面增加功能 | `hakimi.console.setup_api` 接收 HTTP 参数，`hakimi.setup.settings` 校验和写入实例配置 | 命令行设置也使用同一套配置逻辑 |
| 调整记忆、关系、语音或日记 | 分别看 `hakimi.memory`、`hakimi.social`、`hakimi.voice`、`hakimi.features.diary_daily` | B 站观看程序在独立的 `bili_companion` 包，Windows 桌宠在 `desktop_pet/` |

例如，遇到同一人连续补充两句话，先看 `bridge/turn_coordinator.py` 的轮次状态和 `bridge/followup.cpp` 的文本判定。如果回复发出了却没有收到 OneBot 确认，再看 `transport/onebot_websocket.py`。

## 代码与数据分离

| 位置 | 内容 |
|---|---|
| `/opt/hakimi/app` | 代码：本仓库的 git 检出，以可编辑方式安装进 `/opt/hakimi/venv` 和语义向量服务的 venv |
| `/opt/hakimi/data`（**root**） | 运维数据：根配置、`state/`（Terra 巡查、Vertex 会话、Codex 求助、日记…）、旧记忆 `memory.md` |
| `…/official_test_runtime`（**runtime**） | 机器人通道数据：运行配置、记忆库 `state/memory.sqlite3`、发送记录、表情包 |

目录名 `official_test_runtime` 是历史名称，它就是正式运行目录。

`hakimi.paths` 统一回答“东西在哪”：

- `paths.root()`：`HAKIMI_ROOT`，未设置时源码检出目录自身充当 root（开发和测试）。
- `paths.runtime()`：`<root>/official_test_runtime`。
- `paths.base()`：**当前进程**的数据目录，由 `HAKIMI_BASE` 指定。机器人通道（agent、worker、面板、向量服务）是 runtime，运维服务（巡查、Codex 求助、日记）是 root。
- `paths.persona(name, base)`：风格文件，实例目录里有同名文件就用实例的，否则用包内的。
- `paths.venv_bin()/dsh_bin()/codex_executable()/gateway_dir()`：外部工具位置（`HAKIMI_OPT`、`HAKIMI_GATEWAY_DIR` 可改）。

重构前，每个模块用 `Path(__file__).parent` 找数据，同一份代码在根目录和运行目录各复制一份，靠“副本在哪”区分。现在只有一份代码，由环境变量说明数据目录。

## 历史入口文件

有些调用方按路径执行脚本，这些位置保留着由 `hakimi.ops.shims` 生成的几行转发文件：

- `runtime/qq_cli.py`、`root/qq_cli.py`：模型执行 `python qq_cli.py call …` 调工具；
- `root/cloud_activity.py`、`root/cloud_supervisor.py`：本地 Windows 的 `cloud.cmd` 和 `pull_cloud_status.py` 经 SSH 调用。

转发文件会把数据目录固定为自己所在目录（与原来的代码副本一致），然后运行包内模块。不要手改，执行 `hakimi shims` 重新生成。

## 旧模块名

新代码从实际所属包导入。历史脚本仍可使用 `import memory_store`；网页模块原有导入路径（如 `hakimi.models.vertex_web`）也可继续使用。`hakimi._legacy` 将这些路径按需解析到同一个模块对象，因此缓存、测试替换和配置状态可以共享。

`paths.source_file('vertex_web.py')` 可按旧文件名找到当前实现。旧的网页工作进程也保留可执行入口 `python -m hakimi.models.web_fetch_worker`。

## 分层与依赖

主要模块按下表分工。兼容入口保留已有导入路径。

| 原文件 | 拆成 |
|---|---|
| `official_transport.py` | `official_base`（错误、常量）、`official_routes`（路由库）、`official_api`（HTTP）、适配器本体 |
| `identity_links.py` | `identity_base / store / old_catalog / new_catalog / edit / legacy` |
| `memory_store.py` | `store_base`（表结构、工具函数），以及混入类 `store_write / store_embeddings / store_maintenance / store_retrieval / store_read` |
| `memory_social.py` | `social_base / social_schema / social_fields / social_record / social_repair / social_query / interaction_familiarity / social_seed` |
| `memory_graph.py` | `graph_index`（写入索引），查询留在原文件 |
| `memory_console.py` | `console_db / console_auth / payloads_memory / payloads_social / payloads_identity`，HTTP 服务留在原文件 |
| `personal_review.py` | `review_state / review_select` |
| `fallback_supervisor.py` | `fallback_prompt / fallback_commit / fallback_memory` |
| `prompt_context.py` | `core.jsonio`、`prompt.group_profiles` |

记忆的写入方向：`memory_store` → `graph_index`、`social_schema`（建表、写入时索引）。关系评分和图谱查询在上层，通过 `store()` 工厂使用存储。

OneBot 的消息格式、图片缓存和 WebSocket 收发分别在 `transport/onebot_media.py`、`transport/onebot_websocket.py`。`bridge/bridge_daemon.py` 持有运行中的会话和连接状态，向适配器传入所需队列及回调。

## 提示词与人设

全部在 `src/hakimi/persona/`，代码只按文件名读取（`persona.text(name)`），文本逐字使用。详见该目录的 README。

## 进程

| systemd 单元 | 命令 | 数据目录 |
|---|---|---|
| hakimi-worker | `python -m hakimi.ops.cloud_service worker` → `hakimi.transport.official_lane` | runtime |
| hakimi-agent | `python -m hakimi.ops.cloud_service agent`（Gemini 主回复） | runtime |
| hakimi-help | `… cloud_service help` → `hakimi.features.codex_help_service` | root |
| hakimi-diary（定时 22:30） | `… cloud_service diary` → `hakimi.features.diary_daily` | root |
| hakimi-terra-watch（每 5 分钟） | `python -m hakimi.ops.cloud_supervisor` | root |
| hakimi-memory-console | `python -m hakimi.console.memory_console`，端口 18768 | runtime |
| hakimi-memory-embedding | 向量服务 venv 下 `python -m hakimi.memory.memory_embedding_service` | runtime |

`hakimi-gcp-gateway`（Vertex 网关）和 `hakimi-keyring` 不在本仓库内。

## 按用户限制回复成本

`bridge/user_reply_quota.py` 提供纯本地的滚动窗口计数：同一 `user_id` 在任意30分钟内最多7轮，群聊和私聊共用，Gemini和DSH共用。不同通道若给出不同身份ID，仅能按实际可识别的ID计数。

`TurnCoordinator.publish` 在创建模型任务前检查；领取任务和提交回复再次检查，工具指定 `reply_to` 时以实际可见消息作者为准。一个提交的回复轮次无论拆成多少气泡、图片或文件都只计一次；静默、内省和未提交的失败不占次数。没有指向用户的冷场话题不占某个用户的额度。

计数保存在实例 `state/turn_state.json` 的 `user_reply_history`，沿用协调器跨进程锁、提交意图及恢复机制，重启不会清零。成功写入发送队列即占用次数（发送失败也保留该次额度，避免重试超量）；到期逐条释放。首次升级从最近发送队列的有界尾部导入可识别的用户记录。达到限额时静默停止该机会，不发额外的限额提醒。

常量为 `WINDOW_SECONDS=1800`、`MAX_REPLIES=7`。无需额外依赖、环境变量或配置。通过现有 `python tests/run_tests.py test_user_reply_quota` 运行离线回归，兼容普通检出、可编辑安装和wheel包。
