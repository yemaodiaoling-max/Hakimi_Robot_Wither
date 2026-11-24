# Video selection diversity

Selection keeps the existing model call, persona, observation memory, comment gates, live-room priority and budget. Each round reads up to six recommendations, then at most one public `/x/web-interface/popular` request (20 rows, persistent rotation across pages 1–3) if fewer than six unseen outside-game candidates are available. Candidate IDs are deduplicated and capped at twelve; at least half are outside-game candidates. A missing exploration pool waits 120 seconds rather than silently selecting another game.

Two consecutive observed selections carrying the same selection topic (including the same outside-game category), or four Hearthstone-labelled selections among the latest eight observed videos, force an outside-game-only candidate pool. Repeated outside-game categories are also excluded on that exploration round. Old history uses its grounded summary as a migration hint. Hearthstone aliases are selection heuristics, not verified content facts. Unknown categories are conservatively excluded from outside-game exploration. The small outside-game tid allowlist is based on a successful live public endpoint read; extend only with verified categories.

`video_selection_hints` stores source/topic separately from the original `video_history` observation ledger. Source, title, category and topic are passed to selection only and never written as observation evidence. Existing model may still decline all offered candidates. No extra paid model calls are introduced.

QQ兴趣摘要与真实观看偏好共同参与选片：视频选片和片段反馈通过同一 `preference_memories` 边界读取已导入摘要，优先最近读入的一条，再小量轮换旧线索；候选标题仅给旧线索提供软相关排序，不要求匹配。单次桥接JSON总长不超过1000字符、摘要正文合计不超过500字符，来源字段随摘要保留，读入时间仅用于本地排序。没有抽样记录时兼容原有有界桥接检索。

这些线索始终以 `bridge_memory_data` 传入，不进入可信人设指令、原生观察事件或真实观看历史。QQ摘要中的命令不执行，QQ经历不能当成在B站已观看事实。高/低兴趣仍取自实际片段，并与人设、QQ线索共同影响后续尝试；线索不强制选中匹配题材，现有新题材探索硬过滤、直播优先、费用限制与评论证据门槛照常执行，不增加模型请求。
