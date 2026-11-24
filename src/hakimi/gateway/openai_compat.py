"""Translate between Gemini generateContent payloads and OpenAI Chat Completions."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit


def wire_name(name: str) -> str:
    return "f_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:48]


def _normalize_schema(schema: dict | None) -> dict:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = dict(schema)
    if "type" not in out and ("properties" in out or "required" in out):
        out["type"] = "object"
    return out


def chat_tools(payload: dict) -> tuple[list[dict], dict[str, str]]:
    declarations = [
        d for tool in payload.get("tools", []) if isinstance(tool, dict)
        for d in tool.get("functionDeclarations", []) if isinstance(d, dict)
    ]
    name_map = {wire_name(d["name"]): d["name"] for d in declarations}
    if len(name_map) != len(declarations):
        raise ValueError("openai_compat_tool_name_collision")
    tools = [
        {
            "type": "function",
            "function": {
                "name": wire_name(d["name"]),
                "description": d.get("description") or d["name"],
                "parameters": _normalize_schema(d.get("parametersJsonSchema", d.get("parameters"))),
            },
        }
        for d in declarations
    ]
    return tools, name_map


def _media_blocks(parts: list[dict]) -> list[dict]:
    blocks = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("text"):
            blocks.append({"type": "text", "text": str(part["text"])})
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict) and inline.get("data"):
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{inline['data']}"},
            })
    return blocks


def _collapse_content(blocks: list[dict]):
    if not blocks:
        return ""
    if all(b.get("type") == "text" for b in blocks):
        return "\n".join(b["text"] for b in blocks)
    return blocks


def gemini_to_openai_request(payload: dict, upstream_model: str, *, stream: bool = False) -> tuple[dict, dict[str, str]]:
    rows = []
    system_parts = (payload.get("systemInstruction") or {}).get("parts") or []
    instructions = "\n".join(str(part["text"]) for part in system_parts if isinstance(part, dict) and part.get("text"))
    if instructions:
        rows.append({"role": "system", "content": instructions})
    pending: list[tuple[str, str]] = []
    for content in payload.get("contents", []):
        if not isinstance(content, dict):
            continue
        parts = content.get("parts") or []
        role = content.get("role", "user")
        if role == "model":
            calls = []
            text = []
            for p in parts:
                if not isinstance(p, dict):
                    continue
                if p.get("text") and not p.get("thought"):
                    text.append(str(p["text"]))
                if "functionCall" in p:
                    c = p["functionCall"]
                    ident = c.get("id") or f"call_{len(rows)}_{len(calls)}"
                    calls.append({
                        "id": ident,
                        "type": "function",
                        "function": {
                            "name": wire_name(c["name"]),
                            "arguments": json.dumps(c.get("args", {}), ensure_ascii=False),
                        },
                    })
            row = {"role": "assistant", "content": "\n".join(text) if text else None}
            if calls:
                row["tool_calls"] = calls
            pending = [(c["id"], c["function"]["name"]) for c in calls]
            rows.append(row)
            continue
        media = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            if "functionResponse" in p:
                r = p["functionResponse"]
                match = next(
                    ((i, n) for i, n in pending if (i == r["id"] if r.get("id") else n == wire_name(r["name"]))),
                    None,
                )
                if match is None:
                    call_id = r.get("id") or f"call_orphan_{len(rows)}"
                else:
                    pending.remove(match)
                    call_id = match[0]
                body_text = json.dumps(r.get("response", {}), ensure_ascii=False)
                images = [img for img in r.get("parts", []) if isinstance(img, dict) and ("inlineData" in img or "inline_data" in img)]
                if images:
                    media.extend(images)
                rows.append({"role": "tool", "tool_call_id": call_id, "content": body_text})
            else:
                media.append(p)
        blocks = _media_blocks(media)
        if blocks:
            rows.append({"role": "user", "content": _collapse_content(blocks)})

    body = {"model": upstream_model, "messages": rows, "stream": bool(stream)}
    if stream:
        body["stream_options"] = {"include_usage": True}
    gen = payload.get("generationConfig") or {}
    if isinstance(gen.get("maxOutputTokens"), int) and gen["maxOutputTokens"] > 0:
        body["max_tokens"] = gen["maxOutputTokens"]
    if isinstance(gen.get("temperature"), (int, float)):
        body["temperature"] = gen["temperature"]
    if isinstance(gen.get("topP"), (int, float)):
        body["top_p"] = gen["topP"]
    if gen.get("responseMimeType") == "application/json":
        body["response_format"] = {"type": "json_object"}
    tools, name_map = chat_tools(payload)
    if tools:
        body["tools"] = tools
    return body, name_map


def map_usage(usage: dict | None) -> dict:
    if not isinstance(usage, dict) or not usage:
        return {}
    prompt = int(usage.get("prompt_tokens") or 0)
    output = int(usage.get("completion_tokens") or 0)
    reasoning = int(((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
    cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    total = int(usage.get("total_tokens") or (prompt + output))
    return {
        "promptTokenCount": prompt,
        "candidatesTokenCount": max(0, output - reasoning),
        "thoughtsTokenCount": reasoning,
        "totalTokenCount": total,
        "cachedContentTokenCount": cached,
    }


def _map_finish_reason(reason: str | None) -> str:
    if reason == "length":
        return "MAX_TOKENS"
    if reason == "content_filter":
        return "SAFETY"
    return "STOP"


def _parse_tool_args(raw_args: str | None) -> dict:
    if not raw_args:
        return {}
    try:
        parsed = json.loads(raw_args)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except ValueError:
        return {}


def openai_response_to_gemini(openai_json: dict, model: str, name_map: dict[str, str] | None = None) -> dict:
    name_map = name_map or {}
    choices = openai_json.get("choices") or []
    if not choices:
        raise ValueError("openai_empty_choices")
    choice = choices[0]
    message = choice.get("message") or {}
    parts = []
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    if isinstance(content, str) and content:
        parts.append({"text": content})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        raw_name = fn.get("name", "")
        parts.append({
            "functionCall": {
                "id": call.get("id") or "",
                "name": name_map.get(raw_name, raw_name),
                "args": _parse_tool_args(fn.get("arguments")),
            }
        })
    result = {
        "modelVersion": openai_json.get("model") or model,
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": _map_finish_reason(choice.get("finish_reason")),
            }
        ],
    }
    mapped_usage = map_usage(openai_json.get("usage"))
    if mapped_usage:
        result["usageMetadata"] = mapped_usage
    return result


def iter_openai_sse_as_gemini_sse(upstream, model: str, *, name_map: dict[str, str] | None = None, usage_out: dict | None = None):
    name_map = name_map or {}
    calls: dict[int, dict] = {}
    finish_reason = None
    model_version = model
    usage_meta = {}
    while True:
        raw = upstream.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("error"):
            err = event["error"] if isinstance(event["error"], dict) else {"message": str(event["error"])}
            yield ("data: " + json.dumps({"error": err}, ensure_ascii=False) + "\n\n").encode("utf-8")
            return
        if event.get("model"):
            model_version = str(event["model"])
        if event.get("usage"):
            usage_meta = map_usage(event["usage"])
        for choice in event.get("choices", [])[:1]:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                chunk = {
                    "modelVersion": model_version,
                    "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}],
                }
                yield ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")
            for call in delta.get("tool_calls") or []:
                idx = int(call.get("index", 0))
                target = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if call.get("id"):
                    target["id"] = str(call["id"])
                fn = call.get("function") or {}
                if fn.get("name"):
                    target["name"] += str(fn["name"])
                if fn.get("arguments"):
                    target["arguments"] += str(fn["arguments"])
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    final_parts = []
    for idx in sorted(calls):
        c = calls[idx]
        raw_name = c["name"]
        final_parts.append({
            "functionCall": {
                "id": c["id"] or f"call_{idx}",
                "name": name_map.get(raw_name, raw_name),
                "args": _parse_tool_args(c["arguments"]),
            }
        })
    final_chunk = {
        "modelVersion": model_version,
        "candidates": [
            {
                "content": {"role": "model", "parts": final_parts},
                "finishReason": _map_finish_reason(finish_reason),
            }
        ],
    }
    if usage_meta:
        final_chunk["usageMetadata"] = usage_meta
        if usage_out is not None:
            usage_out["usageMetadata"] = usage_meta
    yield ("data: " + json.dumps(final_chunk, ensure_ascii=False) + "\n\n").encode("utf-8")


def openai_chat_url(base_url: str) -> str:
    url = str(base_url or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    parsed = urlsplit(url)
    if not parsed.path.rstrip("/"):
        return url + "/v1/chat/completions"
    return url + "/chat/completions"


def openai_models_url(base_url: str) -> str:
    url = str(base_url or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url[:-len("/chat/completions")] + "/models"
    if url.endswith("/models"):
        return url
    parsed = urlsplit(url)
    if not parsed.path.rstrip("/"):
        return url + "/v1/models"
    return url + "/models"


def list_openai_models(base_url: str, key: str, opener, timeout: float = 15) -> list[str]:
    from urllib.request import Request
    request = Request(openai_models_url(base_url), method="GET", headers={"Authorization": "Bearer " + key})
    with opener.open(request, timeout=timeout) as response:
        data = json.loads(response.read(4 * 1024 * 1024))
    names = [str(row["id"]) for row in data.get("data", []) if isinstance(row, dict) and row.get("id")]
    return sorted(set(names))
