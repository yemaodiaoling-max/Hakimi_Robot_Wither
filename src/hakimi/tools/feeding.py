"""Owner-only virtual feeding. One bridge writes; both models read the receipt."""
import json
import re
import secrets
import time

from hakimi.core.runtime_io import read_json_cached

ITEM = r"[\u4e00-\u9fffA-Za-z]{0,12}罐头"
NUMBER = r"[1-9][0-9]{0,3}|[一二两三四五六七八九十]"
COUNTS = dict(zip("一二两三四五六七八九十", (1, 2, 2, 3, 4, 5, 6, 7, 8, 9, 10)))
NATURAL = re.compile(rf"(?:给你|给哈基米|喂你|投喂)\s*({NUMBER})\s*(?:个|罐|份)\s*({ITEM})[!！。]?")
COMMAND = re.compile(rf"/?投喂(?:(罐头)|\s+({ITEM}))(?:\s*([1-9][0-9]{{0,3}}))?[!！。]?")
BIND = re.compile(r"/绑定投喂\s+([0-9a-f]{8})")


def own_text(text, self_id=None):
    # Strip the reply marker, never the quoted/forwarded message it references.
    text = re.sub(r"^\[CQ:reply,id=-?\d+\]\s*", "", str(text).strip())
    if self_id is not None:
        text = re.sub(rf"^\[CQ:at,qq={re.escape(str(self_id))}\]\s*", "", text)
    return re.sub(r"^@哈基米(?:✨(?:Gemini)?)?(?=\s|[，,:：/])\s*[，,:：]?\s*", "", text).strip()


def parse_feed(text):
    if len(text) > 80 or "\n" in text or "\r" in text:
        return None
    match = NATURAL.fullmatch(text)
    if match:
        number, item = match.groups()
        return item, COUNTS[number] if number in COUNTS else int(number)
    match = COMMAND.fullmatch(text)
    if match:
        plain, item, number = match.groups()
        return plain or item, int(number or 1)
    return None


def _save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def arm_binding(base):
    """Local setup only; never replace an already bound account."""
    path = base / "state/feeding.json"
    state = read_json_cached(path, {})
    if state.get("owner_user_id"):
        raise ValueError("feeding account already bound")
    code = secrets.token_hex(4)
    state.update(version=1, binding_code=code)
    _save(path, state)
    return code


def record_interaction(text, session, user_id, message_id, base, *, self_id=None, now=None):
    text = own_text(text, self_id)
    gift, binding = parse_feed(text), BIND.fullmatch(text)
    if (not gift and not binding) or message_id is None:
        return False
    path = base / "state/feeding.json"
    state = read_json_cached(path, {})
    user_id, message_id = str(user_id), str(message_id)
    if binding:
        code = state.get("binding_code", "")
        if state.get("owner_user_id") or not code or not secrets.compare_digest(code, binding[1]):
            return False
        state.update(owner_user_id=user_id, total=0, recent=[])
        state.pop("binding_code", None)
    else:
        if user_id != state.get("owner_user_id"):
            return False
        recent = state.get("recent", [])
        if any(row["session_key"] == session and row["message_id"] == message_id for row in recent):
            return False
        item, count = gift
        receipt = {"session_key": session, "message_id": message_id, "user_id": user_id,
                   "item": item, "count": count, "at": time.time() if now is None else now}
        state.update(total=state.get("total", 0)+count, last=receipt, recent=(recent+[receipt])[-64:])
    _save(path, state)
    return True


def context_for(session, messages, target, base):
    text = str(target.get("text", ""))
    about_feeding = any(word in text for word in ("罐头", "投喂"))
    try:
        state = read_json_cached(base / "state/feeding.json", {}, readonly=True)
    except (OSError, ValueError):
        return {"status": "unavailable"} if about_feeding else None
    owner = state.get("owner_user_id")
    result = {}
    if owner and str(target.get("user_id", "")) == owner:
        result.update(user_id=owner, total=state.get("total", 0))
        if state.get("last"):
            result["last"] = {key: state["last"][key] for key in ("item", "count", "at")}
    elif about_feeding:
        result["status"] = "other_account" if owner else "not_bound"
    visible = {(str(row.get("message_id", "")), str(row.get("user_id", ""))) for row in messages}
    receipts = [{key: row[key] for key in ("message_id", "user_id", "item", "count")}
                for row in state.get("recent", []) if row["session_key"] == session
                and (row["message_id"], row["user_id"]) in visible]
    if receipts:
        result["visible_receipts"] = receipts
    return result or None


def guidance(context):
    feeding = context.get("feeding")
    if not feeding:
        return ""
    status = {"not_bound": "尚未绑定专属投喂账号，暂不计账。",
              "other_account": "本轮发言者不是已绑定的投喂账号，可接梗但不能把他的赠送记到账上。",
              "unavailable": "账本暂不可读，不编造收据。"}.get(feeding.get("status"), "")
    return ("\nfeeding是程序登记的虚拟投喂回执，只计已绑定的专属账号。" + status +
            "按回执自然接住投喂，保持原来语气；可嘴硬，但不能把已收到的罐头说成空气或否认赠送。"
            "只在相关时提起，不反复报数、索要或重复确认旧回执；total是累计收到数，不是剩余库存。"
            "无记录不代表过去从没被喂过。\n")
