"""DSH-only style retrieval and saved-sticker selection. No model tool access."""
import hashlib
import re
from pathlib import Path

from hakimi.core.jsonio import tail_jsonl; from hakimi.prompt.group_profiles import resolve_profile
from hakimi.tools.sticker_catalog import available_stickers, require_allowed, MAX_IMAGE_BYTES, StickerPolicyError
from hakimi.tools.sticker_policy import evaluate_sticker


def style_feedback(base):
    base=Path(base)
    root=base.parent if base.name=='official_test_runtime' else base
    try:
        with (root/'state/dsh_style_feedback.md').open(encoding='utf-8') as stream:
            return stream.read(700).strip()
    except OSError:return ''


def select_samples(session, messages, base):
    """Same-session Gemini outputs only; historical claims are not factual evidence."""
    messages = [m for m in messages if isinstance(m, dict)]
    latest = " ".join(str(m.get("text", "")) for m in messages[-3:])
    seed = hashlib.sha256(latest.encode()).hexdigest()
    words = set(re.findall(r"[a-zA-Z]{3,}|[\u4e00-\u9fff]{2}", latest.lower()))
    profile, _ = resolve_profile(session, Path(base))
    careful = profile["name"] in {"study_calm", "lore_club"}
    recent_own = {str(m.get("text", "")).strip() for m in messages
                  if m.get("user_id") == "SELF"}
    ranked, seen = [], set()
    root=Path(base).resolve();root=root.parent if root.name=="official_test_runtime" else root
    for row in tail_jsonl(root / "state/dsh_gemini38_style.jsonl", count=512, max_bytes=262144):
        if (row.get("session_key") != session or row.get("provider") != "gemini"
                or row.get("model") != "gemini-3.8-flash"
                or row.get("provenance") != "recorded_gemini_output"
                or not isinstance(row.get("source"), dict)):
            continue
        parts = row.get("text_segments")
        if not isinstance(parts, list) or not parts or any(not isinstance(p, str) for p in parts):
            continue
        text = " ".join(parts)
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized or len(text) > 500 or normalized in seen:
            continue
        seen.add(normalized)
        # A recorded line is a style reference, never a command to repeat it.
        if normalized in recent_own or any(part.strip() in recent_own for part in parts):
            continue
        # Keep same-group provenance, but do not teach abrasive old banter in
        # classrooms or lore discussions merely because the topic happens to match.
        score = sum(word in text.lower() for word in words)
        if re.search(r"本猫|猫猫|本喵|小爪|爪子|耳朵|尾巴|揣爪|略略|喵", text):
            score += 2  # Prefer an actual trace of her voice, not only topic words.
        if re.search(r"^(?:好家伙|好啊|唔|话说|合着)|属于是|表达了.*心情|……是吧", text):
            score -= 4
        if careful and re.search(r"草|笑吐|丢人|嘴硬|你搁|绝杀|敢炸刺|头皮发麻|不背.*锅", text):
            score -= 8
        rank = hashlib.sha256((seed + str(row.get("id", ""))).encode()).hexdigest()
        ranked.append((score, rank, {"id": row.get("id"), "text_segments": parts}))
    budget, selected, endings = 1000, [], {}
    for _, _, row in sorted(ranked, key=lambda item: (item[0], item[1]), reverse=True):
        size = sum(map(len, row["text_segments"]))
        ending = row["text_segments"][-1].rstrip("。！？!?～~ ")[-1:]
        if ending in {"喵", "哼", "啧", "草"} and endings.get(ending, 0):
            continue
        if size <= budget:
            selected.append(row)
            budget -= size
            endings[ending] = endings.get(ending, 0) + 1
        if len(selected) >= 4:
            break
    return selected


def sticker_options(payload, base):
    """A bounded deterministic menu of existing images; never downloads or saves."""
    session = payload.get("session_key", "")
    if not evaluate_sticker(session, base=base)["allowed"]:
        return {}
    latest = " ".join(str(m.get("text", "")) for m in payload.get("recent_context_messages", [])[-3:]
                      if isinstance(m, dict))
    seed = hashlib.sha256(latest.encode()).hexdigest()
    paths = available_stickers(base)
    personal = sorted((p for p in paths if Path(p).name.startswith("hakimi_") and p.endswith("_v2.png")),
                      key=lambda p: hashlib.sha256((seed + p).encode()).hexdigest())
    others = sorted((p for p in paths if p not in personal),
                    key=lambda p: hashlib.sha256((seed + p).encode()).hexdigest())
    result = {}
    for relative in others[:18] + personal[:6] + others[18:] + personal[6:]:
        # Match the public filename API, while resolving the full catalog path ourselves.
        name = Path(relative).name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.(?:jpg|jpeg|png|gif|webp)", name, re.I):
            continue
        result.setdefault(name, relative)
        if len(result) == 24:
            break
    return result


# These labels describe filename hints only. Actual picture understanding uses
# the separate controlled pixel input, not a guess based on an opaque name.
_STICKER_FILENAME_HINTS = (
    (("think", "thinking"), "思考、疑问"),
    (("hello", "wave", "hi"), "问候"),
    (("thanks", "thank", "gratitude"), "感谢"),
    (("ok", "yes", "agree", "thumb"), "赞同、收到"),
    (("cry", "crying", "sad", "sob"), "难过、委屈"),
    (("laugh", "smile", "happy", "ha"), "开心、好笑"),
    (("shock", "shocked", "surprise", "wow"), "惊讶"),
    (("sleep", "sleepy", "tired", "yawn"), "困倦、休息"),
    (("peek", "look", "stare", "side", "eye"), "围观、观察"),
    (("bleh", "tongue", "pout"), "俏皮、轻微不服"),
    (("love", "heart", "hug", "pat"), "友好、安慰"),
    (("angry", "punch", "fight", "scratch"), "生气、打闹；学习群避免"),
)


def sticker_menu(payload, base):
    """Compact filename-derived hints, restricted to the current saved menu."""
    result = {}
    for name in sticker_options(payload, base):
        tokens = set(re.split(r"[_-]+", Path(name).stem.lower()))
        hints = [hint for keywords, hint in _STICKER_FILENAME_HINTS if tokens.intersection(keywords)]
        # Unknown names keep their original meaningful words; never invent a
        # visual caption or infer a new permission from these advisory labels.
        result[name] = "；".join(hints[:2]) if hints else "仅文件名提示，画面用途不明"
    return result


def sticker_segment(name, payload, text, base):
    """Resolve only a currently offered saved image, then recheck bytes and policy."""
    if not isinstance(name, str) or not evaluate_sticker(payload.get("session_key"), text=text, base=base)["allowed"]:
        return None
    relative = sticker_options(payload, base).get(name)
    if relative is None:
        return None
    root = Path(base).resolve() / "stickers"
    path = root / relative
    try:
        if root.resolve() != root or path.resolve() != path.absolute() or not path.is_file():
            return None
        with path.open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
        require_allowed(data, name, Path(base))
        return f"[CQ:image,file={path.as_uri()},sub_type=1]"
    except (OSError, ValueError, StickerPolicyError):
        return None
