# 架构说明

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

每个模块的**文件名都没变**，只是按职责放进了子包，例如 `bridge_daemon.py` 现在是 `hakimi/bridge/bridge_daemon.py`。`import memory_store` 这类旧写法由 `hakimi._legacy` 继续支持，新旧名字指向同一个模块对象。这样以下几处都不受影响：

- 故障追踪里的文件名；
- Sol / Codex 的允许文件清单（`paths.source_file('official_transport.py')` 定位源码）；
- 测试里 `patch('vertex_session.time.time')` 这样的写法。

包内代码一律用完整包名导入，并直接从定义该名字的模块导入，不经过统一出口。

## 分层与依赖

包内 144 个模块之间**没有循环依赖**（测试和 `tools/` 不计）。几处大文件按职责拆开，原文件保留为统一出口，再导出原来的名字：

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

`bridge/bridge_daemon.py`（约 1900 行）暂未拆：它的测试直接替换模块里的全局变量（`engine`、`load_config`、图片缓存目录等），拆开要连同测试一起改写。

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
