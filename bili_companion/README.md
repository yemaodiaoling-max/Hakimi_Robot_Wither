# 视频哈基米（bili_companion）

哈基米的 B 站形态：自己刷推荐视频、按兴趣决定看下去还是划走、盯一个优先直播间（开播时抢占普通视频），看过的内容整理成记忆，并通过记忆桥与 QQ 哈基米共享。

- 模型调用全部经过主项目的本地网关（`hakimi gateway`），不单独保存模型 Key。
- B 站登录在主项目的设置面板里完成（扫码或粘贴 Cookie），加密保存在 `state/private/auth/`。
- 预算：视频观看有独立的日/月上限（默认 1 / 10 美元，面板里可调低）。
- 弹幕只会发到你指定的优先直播间（默认关闭），每 90 秒最多 1 条、每小时最多 12 条；视频评论默认关闭，开启后每天最多 2 条。

## 安装

通常由主项目的 `deploy/install.sh --modules video` 完成，等价于：

```bash
.venv/bin/pip install -e "./bili_companion[auth]"
sudo apt install ffmpeg
```

设置面板保存后会生成 `deploy/watcher.json`、`deploy/bridge.json`，并启动 `hakimi-bilibili-watcher`、`hakimi-memory-bridge.timer`。

## 离线体验与测试

```bash
cd bili_companion
PYTHONPATH=src python -m unittest discover -s tests
PYTHONPATH=src python -m bili_companion.runtime.cli replay examples/replay.jsonl --db state/demo.sqlite3
```

更多设计细节见 [docs/architecture.md](docs/architecture.md)、[docs/memory_exchange.md](docs/memory_exchange.md)、[docs/autonomous_video.md](docs/autonomous_video.md)。
