# 指定直播间在看但没发言时

按本场的 `target_changed` 和 `epoch` 查 `watcher_events`。先看是否进入 `WATCHING_PRIORITY_LIVE`，再看是否有 `observed`。单次模型调用模式下，每次返回会留下 `live_decision`；`model_silent` 表示模型选择安静，`cooldown` 表示这轮没开放表达，`invalid`、`expired` 或 `low_confidence` 表示这轮的结果没能成为可发送的弹幕。模型调用本身失败则查 `watch_error`。这些事件不保存观众弹幕正文。

只有 `ready` 才需要继续查 `danmaku` 的发送结果和本场 `danmaku_attempts`。没有尝试记录不能解释为平台拒绝；`sent` 表示发送接口已接受，不保证所有观众的客户端都展示。
