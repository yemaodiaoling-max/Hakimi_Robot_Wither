"""Shared text/JSON completion client via local gateway or custom HTTP API."""
from __future__ import annotations

import json
import re
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from hakimi.gateway import openai_compat
from hakimi.models import aux_config

MAX_RESPONSE_BYTES = 512 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GatewayTextError(RuntimeError):
    """Fixed, credential-free error code."""

    def __init__(self, code: str, *, http_status: int | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _validate_url(url: str) -> str:
    value = str(url or "").strip().rstrip("/")
    if not value:
        raise GatewayTextError("TEXT_API_CONFIG")
    if any(ch in value for ch in "@?#\\ ") or len(value) > 512:
        raise GatewayTextError("TEXT_API_CONFIG")
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.hostname:
        return value
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}:
        return value
    raise GatewayTextError("TEXT_API_CONFIG")


def _extract_gemini_text_and_usage(data: dict, fallback_model: str) -> tuple[str, str, dict]:
    candidates = data.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        raise GatewayTextError("TEXT_API_INVALID_RESPONSE")
    candidate = candidates[0]
    finish = candidate.get("finishReason")
    if finish not in (None, "STOP", "FINISH_REASON_UNSPECIFIED"):
        raise GatewayTextError("TEXT_API_INCOMPLETE")
    parts = (candidate.get("content") or {}).get("parts") or []
    texts = [str(p["text"]) for p in parts if isinstance(p, dict) and p.get("text") and not p.get("thought")]
    if not texts:
        raise GatewayTextError("TEXT_API_INVALID_RESPONSE")
    meta = data.get("usageMetadata") or {}
    prompt_t = int(meta.get("promptTokenCount") or 0)
    cand_t = int(meta.get("candidatesTokenCount") or 0)
    thought_t = int(meta.get("thoughtsTokenCount") or 0)
    usage = {"prompt_tokens": prompt_t, "completion_tokens": cand_t + thought_t}
    if thought_t:
        usage["reasoning_tokens"] = thought_t
    model = str(data.get("modelVersion") or fallback_model)
    return "".join(texts), model, usage


def complete_via_gateway(
    base,
    user_text: str,
    *,
    system: str = "",
    tier: str = "gemini-4-hydrogen",
    json_mode: bool = False,
    max_tokens: int = 4096,
    timeout: float = 60,
    inline_images: list[dict] | None = None,
    route=None,
    opener_factory=None,
) -> dict:
    from hakimi.models import vertex_route

    if route is None:
        route = vertex_route.load(base)
    if not route.enabled:
        raise GatewayTextError("TEXT_GATEWAY_DISABLED")
    try:
        key, cert_path = route.credentials()
    except Exception as exc:
        raise GatewayTextError("TEXT_API_CONFIG") from exc

    parts: list[dict] = [{"text": str(user_text)}]
    for img in inline_images or ():
        if isinstance(img, dict) and img.get("data"):
            parts.append({"inlineData": {"mimeType": img.get("media_type", "image/jpeg"), "data": img["data"]}})
    body: dict = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"maxOutputTokens": int(max_tokens)},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": str(system)}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"

    url = f"{vertex_route.GATEWAY_URL}/v1beta/models/{tier}:generateContent"
    req = Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    if opener_factory is None:
        tls = ssl.create_default_context(cafile=str(cert_path))
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=tls), _NoRedirect())
    else:
        opener = opener_factory()
    try:
        with opener.open(req, timeout=max(1.0, float(timeout))) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        if code in (401, 403):
            raise GatewayTextError("TEXT_API_AUTH", http_status=code) from None
        if code == 429:
            raise GatewayTextError("TEXT_API_QUOTA", http_status=code) from None
        raise GatewayTextError("TEXT_API_HTTP", http_status=code) from None
    except (URLError, TimeoutError, OSError):
        raise GatewayTextError("TEXT_API_UNAVAILABLE") from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise GatewayTextError("TEXT_API_RESPONSE_TOO_LARGE")
    try:
        data = json.loads(raw)
    except ValueError:
        raise GatewayTextError("TEXT_API_INVALID_RESPONSE") from None
    text, reported_model, usage = _extract_gemini_text_and_usage(data, tier)
    return {"text": text, "model": reported_model, "tier": tier, "usage": usage}


def _openai_messages_payload(messages: list[dict], cfg: dict, *, json_mode: bool, max_tokens: int) -> dict:
    model = str(cfg.get("model") or "").strip()
    if not model:
        raise GatewayTextError("TEXT_API_CONFIG")
    limit_param = cfg.get("output_limit_parameter", "max_tokens")
    if limit_param not in {"max_tokens", "max_completion_tokens"}:
        raise GatewayTextError("TEXT_API_CONFIG")
    body = {
        "model": model,
        "messages": messages,
        limit_param: int(max_tokens),
    }
    effort = cfg.get("reasoning_effort")
    if effort in {"off", "low", "medium", "high"}:
        body["reasoning_effort"] = effort
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def complete_via_http_api(
    cfg: dict,
    messages: list[dict],
    *,
    json_mode: bool = False,
    max_tokens: int = 4096,
    timeout: float = 60,
    credentials_file: Path | None = None,
    opener_factory=None,
) -> dict:
    base_url = _validate_url(cfg.get("base_url", ""))
    model = str(cfg.get("model") or "").strip()
    if not model:
        raise GatewayTextError("TEXT_API_CONFIG")
    api_key = aux_config.resolve_api_key(cfg, credentials_file=credentials_file)
    if not api_key:
        raise GatewayTextError("TEXT_API_AUTH")
    protocol = str(cfg.get("protocol") or "openai_compatible")
    if protocol == "gemini":
        system_texts = [m["content"] for m in messages if m.get("role") == "system" and isinstance(m.get("content"), str)]
        contents = []
        for m in messages:
            if m.get("role") == "system":
                continue
            role = "model" if m.get("role") == "assistant" else "user"
            content = m.get("content")
            if isinstance(content, str):
                parts = [{"text": content}]
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if block.get("type") == "text":
                        parts.append({"text": block.get("text", "")})
                    elif block.get("type") == "image_url":
                        url_val = (block.get("image_url") or {}).get("url", "")
                        if url_val.startswith("data:") and ";base64," in url_val:
                            header, b64 = url_val.split(";base64,", 1)
                            parts.append({"inlineData": {"mimeType": header[5:], "data": b64}})
            else:
                parts = [{"text": str(content)}]
            contents.append({"role": role, "parts": parts})
        body: dict = {"contents": contents, "generationConfig": {"maxOutputTokens": int(max_tokens)}}
        if system_texts:
            body["systemInstruction"] = {"parts": [{"text": "\n".join(system_texts)}]}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        url = f"{base_url}/v1beta/models/{model}:generateContent"
        headers = {"Content-Type": "application/json"}
        if cfg.get("auth") == "bearer":
            headers["Authorization"] = "Bearer " + api_key
        else:
            headers["x-goog-api-key"] = api_key
    elif protocol == "openai_compatible":
        body = _openai_messages_payload(messages, cfg, json_mode=json_mode, max_tokens=max_tokens)
        url = openai_compat.openai_chat_url(base_url)
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + api_key}
    else:
        raise GatewayTextError("TEXT_API_CONFIG")

    opener = opener_factory() if opener_factory is not None else build_opener(ProxyHandler({}), _NoRedirect())
    req = Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        with opener.open(req, timeout=max(1.0, float(timeout))) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        if code in (401, 403):
            raise GatewayTextError("TEXT_API_AUTH", http_status=code) from None
        if code == 429:
            raise GatewayTextError("TEXT_API_QUOTA", http_status=code) from None
        raise GatewayTextError("TEXT_API_HTTP", http_status=code) from None
    except (URLError, TimeoutError, OSError):
        raise GatewayTextError("TEXT_API_UNAVAILABLE") from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise GatewayTextError("TEXT_API_RESPONSE_TOO_LARGE")
    try:
        data = json.loads(raw)
    except ValueError:
        raise GatewayTextError("TEXT_API_INVALID_RESPONSE") from None

    if protocol == "gemini":
        text, reported_model, usage = _extract_gemini_text_and_usage(data, model)
        return {"text": text, "model": reported_model, "usage": usage}

    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise GatewayTextError("TEXT_API_INVALID_RESPONSE")
    choice = choices[0]
    if choice.get("finish_reason") not in ("stop", None):
        raise GatewayTextError("TEXT_API_INCOMPLETE")
    message = choice.get("message") or {}
    if message.get("tool_calls") or message.get("function_call") or message.get("refusal"):
        raise GatewayTextError("TEXT_API_INVALID_RESPONSE")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    if not isinstance(content, str) or not content.strip():
        raise GatewayTextError("TEXT_API_INVALID_RESPONSE")
    raw_usage = data.get("usage") or {}
    usage = {}
    if isinstance(raw_usage, dict):
        if type(raw_usage.get("prompt_tokens")) is int:
            usage["prompt_tokens"] = raw_usage["prompt_tokens"]
        if type(raw_usage.get("completion_tokens")) is int:
            usage["completion_tokens"] = raw_usage["completion_tokens"]
        reasoning = (raw_usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if type(reasoning) is int:
            usage["reasoning_tokens"] = reasoning
    return {"text": content, "model": str(data.get("model") or model), "usage": usage}


def _parse_json_object(raw: str) -> dict:
    text = str(raw).strip().lstrip("\ufeff")
    fence = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise GatewayTextError("TEXT_API_INVALID_RESPONSE")
    return data


def complete_structured_ops(base, prompt_text: str, schema: dict, *, model: str | None = None, timeout: float = 90) -> tuple[dict, dict]:
    ops = aux_config.ops_config(base)
    backend = ops.get("backend", "codex_cli")
    if backend == "main_gateway":
        tier = str(ops.get("model") or "").strip() or "gemini-4-hydrogen"
        res = complete_via_gateway(
            base,
            f"{prompt_text}\n\nReturn ONLY one JSON object matching this JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}",
            tier=tier,
            json_mode=True,
            max_tokens=4096,
            timeout=timeout,
        )
        parsed = _parse_json_object(res["text"])
        u = res.get("usage") or {}
        return parsed, {"input_tokens": int(u.get("prompt_tokens") or 0), "output_tokens": int(u.get("completion_tokens") or 0)}
    if backend == "custom_api":
        cfg = dict(ops)
        if not cfg.get("model") and model:
            cfg["model"] = model
        res = complete_via_http_api(
            cfg,
            [{"role": "user", "content": f"{prompt_text}\n\nReturn ONLY one JSON object matching this JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}"}],
            json_mode=True,
            max_tokens=4096,
            timeout=timeout,
        )
        parsed = _parse_json_object(res["text"])
        u = res.get("usage") or {}
        return parsed, {"input_tokens": int(u.get("prompt_tokens") or 0), "output_tokens": int(u.get("completion_tokens") or 0)}
    from hakimi.features.codex_help_runner import run_codex_structured
    return run_codex_structured(prompt_text, base, schema, model=model or ops.get("model") or None, timeout=int(timeout))
