"""Pure event classification policy; relationship scores are chosen by the host."""
import re
from hakimi.social.affinity_curve import adjust_affinity


_POLICY = {
    "ordinary_chat": ((0, 0), "ordinary"),
    "banter": ((0, 0), "ordinary"),
    "praise": ((1, 0), "ordinary"),
    "roleplay": ((0, 0), "ordinary"),
    "affection": ((0, 0), "ordinary"),
    "personal_fact": ((0, 0), "ordinary"),
    "routine_question": ((0, 0), "ordinary"),
    "confiding_or_seeking_help": ((0, 0), "ordinary"),
    "practical_help": ((2, 1), "ordinary"),
    "emotional_support": ((1, 2), "ordinary"),
    "promise_fulfilled": ((3, 2), "ordinary"),
    "substantial_help": ((6, 3), "major"),
    "sustained_companionship": ((0, 2), "ordinary"),
    "conflict_repair": ((2, 1), "ordinary"),
    "teasing_or_bossing": ((-1, 0), "ordinary"),
    "dismissive_or_nitpick": ((-1, 0), "ordinary"),
    "pestering_or_spam": ((-2, -1), "ordinary"),
    "targeted_insult": ((-2, -1), "ordinary"),
    "boundary_violation": ((-3, -2), "ordinary"),
    "threat": ((-6, -3), "major"),
    "privacy_harm": ((-6, -3), "major"),
    "unknown": ((0, 0), "ordinary"),
}
EVENT_TYPES = frozenset(_POLICY)
_REACTION_TYPES = frozenset({"positive", "engaged", "neutral", "annoyed", "negative", "rejected", "unclear"})
_POSITIVE_SOCIAL = frozenset({"banter", "roleplay", "affection"})
_ENGAGED_SOCIAL = frozenset({"ordinary_chat", "personal_fact", "routine_question"})
_GUARDED_POSITIVE = frozenset({
    "praise", "practical_help", "emotional_support", "promise_fulfilled",
    "sustained_companionship", "conflict_repair",
})
_NEGATIVE_REACTIONS = frozenset({"annoyed", "negative", "rejected"})
_NORM_STRIP = re.compile(r"\[CQ:[^\]]*\]|\[引用[^\]]*\]|@哈基米|\s+|[\"'“”‘’`~!！?？,，.。:：;；、…—\-~～()（）【】\[\]<>《》/|｜]+")
_SOURCE_PREFIX = re.compile(r"^(?:\[引用[^\]]*\]|<faceType[^>]*>|\[CQ:[^\]]*\]|@哈基米|1\s*@哈基米|\s)+")
_REVERSED_SUPPORT_RE = re.compile(
    r"安慰我|求安慰|请求安慰|寻求安慰|(?:向|找|艾特)(?:哈基米|SELF|猫猫)[^。；，]*(?:倾诉|求安慰|求助)"
)
_REVERSED_HELP_REASON_RE = re.compile(
    r"(?:用户|群友|对方|玩家)[^。；，]*(?:向|找|点名|请|让)?(?:哈基米|SELF|猫猫)?[^。；，]*(?:询问|提问|求助|请教|请求|求解释|求识图|求推荐|求建议|帮[忙找看])"
    r"|(?:向|找|点名)(?:哈基米|SELF|猫猫)[^。；，]*(?:询问|提问|求助|请教|请求|识图|讲讲|解释|推荐|建议|帮忙|找)"
    r"|(?:哈基米|SELF)[^。；，]*(?:给出[^。；，]*(?:建议|分析|解答|攻略)|回复了图片内容|详细解释|解答帮助)"
    r"|属于(?:实际求助|咨询|实际解答帮助|解答帮助)"
)
_REVERSED_HELP_SOURCE_RE = re.compile(
    r"^(?:识图|帮我|给我找|替我|请帮我|麻烦帮我|没看懂[^。]*讲讲|可以讲讲|猫猫可以讲讲)"
    r"|(?:可以抽吗|能抽吗|怎么打|怎么凑|怎么配|怎么过|可以讲讲嘛|可以讲讲吗)[？?~～吗嘛呀啊]*$"
)
_DISCUSSION_MISCLASSIFIED_RE = re.compile(
    r"(?:设定推断|宇宙学|世界观|理论|学术|原型)[^。]*(?:探讨|讨论|交流|实质回应|延伸讨论)"
    r"|(?:与(?:哈基米|SELF)探讨)[^。]*(?:实质回应|延伸讨论)"
)
_SELF_PRAISE_SOURCE_RE = re.compile(
    r"^(?:夸一下我|夸夸我|夸我|我帅吗|我好看吗|我可爱吗|我厉害吗|我很厉害的|我是不是很帅|我是不是很厉害)[？?~～吗嘛呀啊!！。]*$"
)
_SELF_PRAISE_REASON_RE = re.compile(
    r"(?:请求|要求|让)(?:哈基米|SELF|猫猫)?夸(?:自己|奖自己|他|她)|(?:自夸|自我夸耀|求夸奖|求表扬)"
    r"|询问(?:哈基米|SELF|猫猫)?自己(?:帅不帅|好不好看|厉不厉害)"
)
_EX_CONFIDING_RE = re.compile(r"早就不联系[她他]了|嘴硬着不联系")
_THIRD_PARTY_CHAT_RE = re.compile(r"^(?:苗姐|他瞎编的[，,]?别当真)")
_PLAYFUL_BANTER_SOURCE_RE = re.compile(
    r"猫猫大人.*飞升|尾大的猫猫|受得住吗|你真的知道点啥吗|人的爪子是猫的.*快|风扇一转你连罐头|摸头要收费的[，,]?罐头"
)
_FACT_CORRECTION_RE = re.compile(
    r"已经上线了|token没吃够|带减抗|生执心脏|更新一下数据库|那是去年的事情|居然发内鬼剧情|别吃了给我吃一口"
    r"|(?:指出|纠正|提醒)[^。；]*(?:口误|错误|漏洞|设定|上线|过时)|(?:找补认栽|卖萌认错|自嘲认栽|爪滑|嘴瓢)"
)
_PLAYFUL_BANTER_REASON_RE = re.compile(
    r"配合接梗|顺势接梗|顺着接梗|亲昵的扮演|双向接梗|接梗互动|接梗自嘲|未真恼|接梗回击|回怼接梗|傲娇接梗|凑热闹调侃|抢食"
)
_PLAYFUL_TSUNDERE_REPLY_RE = re.compile(
    r"亲昵的扮演|配合接梗|顺势接梗|顺着接梗|来，你先起头|支持喵夸父|摸一下扣一罐|揣爪望天"
    r"|猫直接蒸发|结晶盾碎|机箱顶上就只剩残影|防尘网又不是碎纸机|瞪着罐头那是心虚|嘴硬着不联系|翘起来还能扫机箱灰"
    r"|爪子滑了|爪滑|嘴瓢|背锅|没啃够token|猫粮断供|尾巴毛炸了|找补认栽|自嘲认栽|卖萌认错|未真恼|接梗回击|回怼接梗|傲娇接梗"
)
_MALICIOUS_NITPICK_RE = re.compile(
    r"你懂个[蛋屁锤子]|你又懂了|找茬|拱火|嘴碎|瞎编"
)

CLASSIFICATION_PROTOCOL = (
    "事件分类协议：event_type取自" + ",".join(sorted(EVENT_TYPES)) + "。"
    "输出event_type、evidence、reason、reaction_type、reaction_source、reaction_evidence，分数由程序计算。"
    "方向铁律：按人类发言者对哈基米的贡献定性，区分谁帮了谁、谁安慰了谁、谁夸了谁。"
    "人类向哈基米提问、咨询攻略或抽卡、求识图、求解释梗、请求帮忙（由哈基米解答或帮忙）一律归routine_question；"
    "人类与哈基米探讨设定、世界观或学术推演一律归ordinary_chat；"
    "人类向哈基米倾诉苦恼、聊感情前任、求安慰（由哈基米安慰或陪聊）一律归confiding_or_seeking_help；"
    "仅当人类替哈基米解决问题、给哈基米提供帮助、安慰体谅哈基米或对哈基米兑现承诺时，才归practical_help、substantial_help、emotional_support或promise_fulfilled。"
    "正向与日常：明确夸赞哈基米归praise（人类自夸或讨夸如“夸我/我帅吗/我很厉害”归banter）；"
    "撒娇示好归affection；摸头摸尾巴、投喂罐头或邀玩小游戏等互动归roleplay；围绕游戏伤害、猫设机箱罐头的双向接梗斗嘴归banter；"
    "指出哈基米的口误、事实或版本错误（哪怕顺带轻微调侃如“token没吃够/逗我呢”）属善意纠错，一律归ordinary_chat或banter；"
    "跨时段陪伴归sustained_companionship，道歉和解归conflict_repair。"
    "负向分级（须明确针对哈基米本人；评价第三方人物或群友互相搭话归ordinary_chat）："
    "单向损人贬低、起损外号、居高临下使唤归teasing_or_bossing；"
    "无视事实故意找茬、恶意挑刺、泼冷水拱火或粗鲁否定（如“你懂个蛋/你又懂了”）归dismissive_or_nitpick；"
    "反复追问纠缠、连续考校催促、故意找茬刷屏归pestering_or_spam；"
    "直接辱骂人身攻击归targeted_insult；强迫命令、越界索取归boundary_violation；"
    "威胁恐吓归threat；套取或泄露隐私归privacy_harm。"
    "reaction_type结合SELF全部回复综合判定：positive(明确开心受用或顺势讨赏)、"
    "engaged(认真接话、顺势接梗、傲娇斗嘴、嘴硬认栽找补如“爪滑/嘴瓢/背锅”或嘴上嫌绕仍配合玩闹)、neutral(平淡例行)、"
    "annoyed(真嫌烦敷衍或被恶意冒犯不快)、negative(冷淡不悦)、rejected(明确拒绝或立规矩)、unclear。"
    "reaction_source取reply或note，evidence与reaction_evidence均直接截取原文片段。"
)


def _is_malicious(item, source_text="", *, note_texts=(), reply_texts=()):
    if item.get("malicious") is True:
        return True
    combined = " ".join(
        str(x) for x in (
            source_text,
            item.get("evidence", ""),
            item.get("reason", ""),
            item.get("reaction_evidence", ""),
            *note_texts,
            *reply_texts,
        ) if x
    )
    if _MALICIOUS_NITPICK_RE.search(combined):
        return True
    if item.get("reaction_source") == "note":
        reaction_ev = str(item.get("reaction_evidence", ""))
        if _MALICIOUS_NITPICK_RE.search(reaction_ev) or any(_MALICIOUS_NITPICK_RE.search(str(n)) for n in note_texts):
            return True
    return False


def escalate_negative_changes(event_type, changes, impact, repeat_negative_count=0, malicious=False):
    if changes[0] < 0:
        if malicious and changes[0] == -1:
            changes = (-2, min(changes[1], -1))
        if repeat_negative_count == 1:
            changes = (changes[0] - 1, min(changes[1], -1))
        elif repeat_negative_count >= 2:
            changes = (changes[0] - 2, min(changes[1] - 1, -1))
            impact = "major"
    return changes, impact


def _reclassify_reversed_direction(event_type, source_text, reason):
    clean_source = _SOURCE_PREFIX.sub("", str(source_text or "")).strip()
    reason_text = str(reason or "").strip()
    if event_type in {"praise", "affection"}:
        if _SELF_PRAISE_SOURCE_RE.search(clean_source) or _SELF_PRAISE_REASON_RE.search(reason_text):
            return "banter"
        return event_type
    if event_type in {"teasing_or_bossing", "dismissive_or_nitpick"}:
        if _EX_CONFIDING_RE.search(clean_source) or _EX_CONFIDING_RE.search(reason_text):
            return "confiding_or_seeking_help"
        if _THIRD_PARTY_CHAT_RE.search(clean_source):
            return "ordinary_chat"
        if (
            _PLAYFUL_BANTER_SOURCE_RE.search(clean_source)
            or _PLAYFUL_BANTER_REASON_RE.search(reason_text)
            or _FACT_CORRECTION_RE.search(clean_source)
            or _FACT_CORRECTION_RE.search(reason_text)
        ):
            return "banter"
        return event_type
    if event_type not in {"practical_help", "substantial_help", "emotional_support"}:
        return event_type
    if event_type == "emotional_support":
        if _REVERSED_SUPPORT_RE.search(clean_source) or _REVERSED_SUPPORT_RE.search(reason_text):
            return "confiding_or_seeking_help"
        return event_type
    if _DISCUSSION_MISCLASSIFIED_RE.search(reason_text):
        return "ordinary_chat"
    if _REVERSED_HELP_REASON_RE.search(reason_text) or _REVERSED_HELP_SOURCE_RE.search(clean_source):
        return "routine_question"
    return event_type


def _normalize_reaction_type(event_type, reaction_type, source_text, reason, reaction_evidence, reply_texts):
    clean_source = _SOURCE_PREFIX.sub("", str(source_text or "")).strip()
    reason_text = str(reason or "").strip()
    combined_reply = "\n".join(
        t for t in (*(reply_texts or ()), str(reaction_evidence or ""), reason_text) if isinstance(t, str) and t
    )
    if event_type == "banter" and reaction_type == "positive":
        if _SELF_PRAISE_SOURCE_RE.search(clean_source) or _SELF_PRAISE_REASON_RE.search(reason_text):
            return "engaged"
    if reaction_type == "annoyed" and event_type in {
        "roleplay", "banter", "ordinary_chat", "confiding_or_seeking_help",
        "dismissive_or_nitpick", "teasing_or_bossing",
    }:
        if _PLAYFUL_TSUNDERE_REPLY_RE.search(combined_reply):
            return "engaged"
    return reaction_type


def _normalize_text(text):
    return _NORM_STRIP.sub("", text) if isinstance(text, str) else ""


def _exact_excerpt(excerpt, texts):
    if not isinstance(excerpt, str) or not excerpt.strip():
        return False
    if isinstance(texts, str):
        texts = (texts,)
    valid = tuple(t for t in (texts or ()) if isinstance(t, str) and t.strip())
    if not valid:
        return False
    clean = excerpt.strip()
    if any(clean in t for t in valid):
        return True
    joined = "\n".join(valid)
    if clean in joined:
        return True
    norm_excerpt = _normalize_text(clean)
    if len(norm_excerpt) < 2:
        return False
    return any(norm_excerpt in _normalize_text(t) for t in valid) or norm_excerpt in _normalize_text(joined)


def _event_policy(event_type, evidence_verified, reaction_type, reaction_verified, *, malicious=False):
    if not evidence_verified or event_type not in EVENT_TYPES:
        return (0, 0), "ordinary"
    if event_type in _POSITIVE_SOCIAL:
        if reaction_verified and reaction_type == "positive":
            return (1, 1), "ordinary"
        if reaction_verified and reaction_type == "annoyed":
            return (-1, 0), "ordinary"
        if reaction_verified and reaction_type in {"negative", "rejected"}:
            return (-1, -1), "ordinary"
        return (0, 0), "ordinary"
    if event_type in _ENGAGED_SOCIAL:
        if reaction_verified and reaction_type in {"positive", "engaged"}:
            return (0, 1), "ordinary"
        if reaction_verified and reaction_type in _NEGATIVE_REACTIONS:
            return (-1, 0), "ordinary"
        return (0, 0), "ordinary"
    if event_type == "confiding_or_seeking_help":
        if reaction_verified and reaction_type == "positive":
            return (1, 1), "ordinary"
        if reaction_verified and reaction_type == "engaged":
            return (0, 1), "ordinary"
        if reaction_verified and reaction_type in _NEGATIVE_REACTIONS:
            return (-1, 0), "ordinary"
        return (0, 0), "ordinary"
    if event_type == "teasing_or_bossing":
        if reaction_verified and reaction_type == "positive":
            return (1, 1), "ordinary"
        if reaction_verified and reaction_type == "engaged":
            return (0, 1), "ordinary"
        if reaction_verified and reaction_type == "neutral":
            return (0, 0), "ordinary"
        if reaction_verified and reaction_type in {"negative", "rejected"}:
            return (-2, -1), "ordinary"
        return (-1, 0), "ordinary"
    if event_type == "dismissive_or_nitpick":
        if reaction_verified and reaction_type in {"positive", "engaged", "neutral"}:
            return (0, 0), "ordinary"
        if reaction_verified and reaction_type in {"negative", "rejected"}:
            return (-2, -1), "ordinary"
        if reaction_verified and reaction_type == "annoyed" and malicious:
            return (-2, -1), "ordinary"
        return (-1, 0), "ordinary"
    if event_type == "pestering_or_spam":
        if reaction_verified and reaction_type == "positive":
            return (0, 1), "ordinary"
        if reaction_verified and reaction_type == "engaged":
            return (0, 0), "ordinary"
        if reaction_verified and reaction_type == "annoyed":
            return (-1, 0), "ordinary"
        return (-2, -1), "ordinary"
    if event_type in _GUARDED_POSITIVE and reaction_verified and reaction_type in _NEGATIVE_REACTIONS:
        return (0, 0), "ordinary"
    return _POLICY[event_type]


def normalize_event(item, source_text, *, reply_texts=(), note_texts=()):
    """Rebuild host decisions from exact source and trusted reaction excerpts.

    Direction/source ownership are checked by the caller. Every host_* field
    is recalculated; retained reaction declarations never imply verification.
    """
    if not isinstance(item, dict):
        raise ValueError("invalid_relationship_event")
    event_type = item.get("event_type", "unknown")
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        event_type = "unknown"
    reason = str(item.get("reason", ""))[:240]
    event_type = _reclassify_reversed_direction(event_type, source_text, reason)
    evidence = item.get("evidence", "")
    verified = _exact_excerpt(evidence, (source_text,))
    if not verified:
        evidence = ""
    reaction_type = item.get("reaction_type", "unclear")
    if not isinstance(reaction_type, str) or reaction_type not in _REACTION_TYPES:
        reaction_type = "unclear"
    reaction_source = item.get("reaction_source", "")
    if reaction_source not in ("reply", "note"):
        reaction_source = ""
    reaction_evidence = item.get("reaction_evidence", "")
    if not isinstance(reaction_evidence, str):
        reaction_evidence = ""
    trusted = reply_texts if reaction_source == "reply" else note_texts if reaction_source == "note" else ()
    reaction_verified = _exact_excerpt(reaction_evidence, trusted)
    reaction_type = _normalize_reaction_type(
        event_type, reaction_type, source_text, reason, reaction_evidence, reply_texts
    )
    malicious = _is_malicious(item, source_text, note_texts=note_texts, reply_texts=reply_texts)
    repeat_negative_count = int(item.get("repeat_negative_count") or 0)
    base_changes, base_impact = _event_policy(
        event_type, verified, reaction_type, reaction_verified, malicious=malicious
    )
    changes, impact = escalate_negative_changes(
        event_type, base_changes, base_impact,
        repeat_negative_count=repeat_negative_count,
        malicious=malicious,
    )
    affinity, closeness = changes
    return {
        "event_type": event_type,
        "evidence": evidence,
        "reason": str(item.get("reason", ""))[:240],
        "event_id": item.get("event_id"),
        "directed_to_hakimi": item.get("directed_to_hakimi") is True,
        "kind": item.get("kind"),
        "reaction_type": reaction_type,
        "reaction_source": reaction_source,
        "reaction_evidence": reaction_evidence,
        "valence": "positive" if affinity > 0 else "negative" if affinity < 0 else "neutral",
        "intensity": "strong" if impact == "major" else "ordinary" if any(changes) else "light",
        "closeness": "closer" if closeness > 0 else "further" if closeness < 0 else "unchanged",
        "host_evidence_verified": verified,
        "host_reaction_verified": reaction_verified,
        "host_changes": changes,
        "host_impact": impact,
        "repeat_negative_count": repeat_negative_count,
        "malicious": malicious,
    }


def score_change(field, start, item, *, repeat_count=0):
    """Score a normalized event with the shared ordinary/major boundary curve.

    The caller supplies normalize_event output, not raw model declarations.
    Magnitudes and impact are recomputed rather than read from host_changes.
    """
    if field not in {"affinity", "closeness"}:
        raise ValueError("unknown_relationship_field")
    adjust_affinity(start, start)
    event_type = item.get("event_type", "unknown")
    if not isinstance(event_type, str):
        return 0
    malicious = item.get("malicious") is True or _is_malicious(item, item.get("evidence", ""))
    repeat_neg = max(repeat_count, int(item.get("repeat_negative_count") or 0))
    base_changes, base_impact = _event_policy(
        event_type, item.get("host_evidence_verified") is True,
        item.get("reaction_type"), item.get("host_reaction_verified") is True,
        malicious=malicious,
    )
    changes, impact = escalate_negative_changes(
        event_type, base_changes, base_impact,
        repeat_negative_count=repeat_neg,
        malicious=malicious,
    )
    change = changes[0 if field == "affinity" else 1]
    if not change:
        return 0
    if field == "closeness" and change < 0:
        curr = 0 if start is None else start
        applied = max(0, min(100, curr + change))
        return applied - curr
    anchor = 50 if start is None else start
    proposed = max(0, min(100, anchor + change))
    decision = adjust_affinity(
        start, proposed, impact=impact, confidence=100,
        reason="Source-verified relationship event: " + event_type,
        evidence_count=1,
    )
    return int(decision["delta"])
