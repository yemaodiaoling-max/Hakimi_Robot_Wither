# 本地模型网关

所有模块都只和本机的 `https://127.0.0.1:18765` 通信（Gemini `generateContent` 协议 + 本地令牌），由网关转发到你配置的上游。好处：

- API Key 只存在网关目录一个地方（`upstream.key`，权限 0600），各模块进程拿不到。
- 统一记账：每次请求的 token 用量写进 `gateway_ledger.sqlite3`，按单价估算花费，可设每月上限。
- 换服务商只改网关配置，模块代码不用动。

## 模型映射

代码里用固定的“档位名”请求模型，网关把它们映射到真实模型名（设置面板「模型与 Key」）：

| 档位 | 用途 | 面板字段 |
|---|---|---|
| `gemini-4-argon` | QQ 主回复 | 主回复模型 |
| `gemini-4-carbon` | 详细推导、考据、看图解题（开启智能分流时） | 强推理模型 |
| `gemini-4-hydrogen`、`gemini-3.8-flash` | 复盘、消息梳理、桌宠、视频选片；智能分流时的日常闲聊 | 轻量模型 |
| `gemini-3.5-flash-lite` | 看视频/直播抽帧 | 视频观察模型 |

映射保存在网关目录的 `gateway.json`：

```json
{
  "upstream": {"base_url": "https://generativelanguage.googleapis.com", "auth": "x-goog-api-key", "proxy": ""},
  "models": {"gemini-4-argon": "你选的主模型", "gemini-4-hydrogen": "你选的轻量模型", "...": "..."},
  "prices": {"你选的主模型": {"input_usd_per_million": "0.3", "output_usd_per_million": "2.5"}},
  "monthly_limit_usd": "20"
}
```

## 兼容地址与代理

- **Gemini / Gemini 兼容**：接口类型选“Google Gemini API”或“Gemini 兼容地址”，鉴权按服务商选 `x-goog-api-key` 或 `Bearer`。
- **OpenAI 兼容（单 API 开箱即可用）**：接口类型选“OpenAI 兼容地址”（如 DeepSeek、OpenRouter、自建 vLLM 等），网关会自动把内部 Gemini `generateContent` 请求与工具调用流式互转为 `/chat/completions` 并照常记账。
- 服务器无法直连 Google 时，在「代理」里填 `http://127.0.0.1:7890` 这类地址。

## 月度上限

`monthly_limit_usd` 为空表示不设上限（仍然记账）。达到上限后网关对生成请求返回 `vertex_monthly_budget_exhausted`，QQ 机器人会改用备用模型（若已配置），视频机器人暂停观看，直到下个月（太平洋时间）。花费按你填写的单价**估算**，以服务商账单为准；单价没填时按 0 计。

## 命令

```bash
hakimi gateway init      # 生成本地令牌与证书（不会覆盖）
hakimi gateway serve     # 前台运行（systemd 单元 hakimi-gateway 就是这个命令）
hakimi gateway status    # 上游、映射、本月请求数与估算花费
hakimi gateway models    # 列出上游 Key 可用的模型
```
