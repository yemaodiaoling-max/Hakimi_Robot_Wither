"""One-shot, zero-tool HTTP fallback responder (OpenAI-compatible / Gemini / gateway)."""
from __future__ import annotations

import base64
import re

from hakimi import persona
from hakimi.models import aux_config, gateway_text
from hakimi.models.fallback_runner import (
    BASE_DIR,
    MAX_PROMPT_BYTES,
    MAX_PROMPT_CHARS,
    FallbackError,
    parse_response,
)


def _validate_images(images) -> list[dict]:
    valid = []
    if not images:
        return valid
    if not isinstance(images, (list, tuple)) or len(images) > 2:
        raise FallbackError("FALLBACK_IMAGES_INVALID")
    for image in images:
        if (not isinstance(image, dict) or image.get("media_type") != "image/jpeg"
                or not re.fullmatch(r"I[1-8]|R1", str(image.get("id", "")))
                or not isinstance(image.get("data"), str) or len(image["data"]) > 700000):
            raise FallbackError("FALLBACK_IMAGES_INVALID")
        try:
            raw = base64.b64decode(image["data"], validate=True)
        except (ValueError, TypeError):
            raise FallbackError("FALLBACK_IMAGES_INVALID") from None
        if len(raw) > 524288:
            raise FallbackError("FALLBACK_IMAGES_INVALID")
        valid.append(image)
    return valid


def _map_error(exc: gateway_text.GatewayTextError) -> FallbackError:
    mapping = {
        "TEXT_API_CONFIG": "FALLBACK_PROVIDER_CONFIG_UNAVAILABLE",
        "TEXT_GATEWAY_DISABLED": "FALLBACK_PROVIDER_CONFIG_UNAVAILABLE",
        "TEXT_API_AUTH": "FALLBACK_CREDENTIAL_UNAVAILABLE",
        "TEXT_API_INCOMPLETE": "FALLBACK_RESPONSE_INVALID",
        "TEXT_API_INVALID_RESPONSE": "FALLBACK_RESPONSE_INVALID",
        "TEXT_API_RESPONSE_TOO_LARGE": "FALLBACK_OUTPUT_TOO_LARGE",
    }
    return FallbackError(mapping.get(exc.code, "FALLBACK_PROCESS_FAILED"))


def run_http_fallback(prompt, base=BASE_DIR, *, timeout_seconds=90, images=None, cfg=None):
    if (not isinstance(prompt, str) or not 1 <= len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES
            or len(prompt) > MAX_PROMPT_CHARS):
        raise FallbackError("FALLBACK_PROMPT_INVALID")
    validated_images = _validate_images(images)
    cfg = dict(cfg) if cfg is not None else aux_config.fallback_config(base)
    from hakimi.persona.prompt_language import prompt as positive_prompt

    system_text = persona.text("dsh_runtime_prefix.txt")
    task = "Return one JSON object with action and text, using the tool protocol in the current task.\n" + positive_prompt(prompt)
    protocol = str(cfg.get("protocol") or "openai_compatible")
    timeout = max(1.0, float(timeout_seconds or 90))
    try:
        if protocol == "main_gateway":
            res = gateway_text.complete_via_gateway(
                base, task, system=system_text,
                tier=str(cfg.get("model") or "").strip() or "gemini-4-hydrogen",
                json_mode=True, max_tokens=2400, timeout=timeout,
                inline_images=validated_images,
            )
        else:
            if validated_images:
                user_content: object = [
                    {"type": "text", "text": task},
                    *[
                        {"type": "image_url", "image_url": {"url": f"data:{img['media_type']};base64,{img['data']}"}}
                        for img in validated_images
                    ],
                ]
            else:
                user_content = task
            messages = [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_content},
            ]
            res = gateway_text.complete_via_http_api(
                cfg, messages, json_mode=True, max_tokens=2400, timeout=timeout,
            )
    except gateway_text.GatewayTextError as exc:
        raise _map_error(exc) from None
    return parse_response(res["text"])
