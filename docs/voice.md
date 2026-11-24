# QQ 语音（可选，进阶）

`hakimi.voice` 提供两件事，默认都关闭：

- **听**：本地 CPU 转写群里的语音消息（只给当前会话使用，原文不进长期记忆）。
- **说**：用户明确要求时，用本地 TTS 发一段语音或短哼唱。

开启方式：在实例目录创建 `voice_config.json`：

```json
{"enabled": true, "tts_enabled": false}
```

并自行安装语音识别 / TTS 依赖（体积较大，不在默认安装里）。启动 `hakimi.voice.worker`（可参考 `deploy/systemd` 里其它单元的写法自建一个）。

## 关于音色

本仓库**不附带任何音色或参考音频**。零样本音色克隆（`tts_worker.py` 的 `zipvoice` 后端）需要你提供参考片段：请只使用你自己的声音，或已获得明确授权、允许用于语音合成的素材；不要使用游戏、动画、主播等他人声音。参考片段放在 `tts_voice_pack` 指定的目录（默认 `/opt/hakimi/models/voice/default`），格式见 `src/hakimi/voice/tts_server.py`。
