"""Two-tier impression memory: Core Anchors + Recent Window - Ephemeral Trivialities.

Pure, deterministic compaction modeling human-like social impression dynamics:
durable identity/personality traits and promises are retained long-term,
while fleeting daily trivia gradually decay and fade over time.
"""
from __future__ import annotations

import re


BOILERPLATE_RE = re.compile(
    r"^(?:"
    r"引用对方关于.*的发言，用玩笑回应|"
    r"向对方交流.*并给出肯定评价|"
    r"(?:当天)?多次公开@对方，其中一次询问.*|"
    r"近期有新的互动细节，顺手在小本子里把印象和好感基[准]?明确下来|"
    r"多次围绕游戏内容请对方检查.*|"
    r"就游戏版本和剧情进度回应对方.*|"
    r"就话题直接回应对方.*|"
    r"引用对方消息，连续追问.*|"
    r"在群里交流挺积极的群友，发言很有个人特色，相处挺舒服的|"
    r"曾向对方回复话题，也曾在发图时提及对方|"
    r"多次回应目标的剧情观点.*|"
    r"就游戏剧情走向多次追问并质疑对方的推论|"
    r"多次点名对方，围绕游戏观看话题互动，也使用昵称|"
    r"回应对方关于模型、称呼和法律检索的发言.*"
    r")[。！？；!?;]*$"
)

CORE_BACKGROUND_SIGNALS = frozenset({
    "研究", "专业", "科研", "方向", "量子", "物理", "声学", "代码", "程序员",
    "考据", "设定", "世界观", "同好", "初恋", "时差", "作息", "博士", "论文", "建模",
})

CORE_PERSONA_SIGNALS = frozenset({
    "性格", "脾气", "人设", "一贯", "平时总是", "日常爱", "老熟人", "饭友", "同伴",
    "护猫", "护短", "有担当", "投缘", "心软", "嘴硬心软", "冷幽默", "毒舌",
    "严谨", "踏实", "细腻", "文雅", "大方", "温柔", "黏人",
})

CORE_SIGNATURE_SIGNALS = frozenset({
    "外号", "称呼", "叫我", "自称", "破盆", "白饭", *__import__("hakimi.core.owner", fromlist=["x"]).local_terms("signature_terms"), "鲸鱼娘",
    "鲷鱼烧", "金枪鱼罐头", "猫猫大人", "约定", "答应", "承诺",
})

ALL_CORE_SIGNALS = CORE_BACKGROUND_SIGNALS | CORE_PERSONA_SIGNALS | CORE_SIGNATURE_SIGNALS

EPHEMERAL_MARKERS = frozenset({
    "今天", "刚才", "刚刚", "这会儿", "昨晚", "前天", "大清早", "下线前", "关机明天见",
    "最近还一口气", "看群里聊", "被卡住时", "查岗问", "问图是啥意思", "猜绿毛", "十七集中二短剧",
})

EPHEMERAL_TIME_START = (
    "今天", "刚才", "刚刚", "昨晚", "这会儿", "大清早", "最近", "前天",
)


def split_clauses(text: str) -> list[str]:
    """Split impression text into sentence clauses on boundary punctuation and newlines."""
    text = str(text or "").strip()
    if not text:
        return []
    raw_pieces = re.split(r"(?<=[。！？；!?;])(?![。！？；!?;])\s*|\n+", text)
    clauses = []
    for piece in raw_pieces:
        p = piece.strip()
        if p and re.search(r"[^\s。！？；!?;~…\-_]", p):
            clauses.append(p)
    return clauses


def is_boilerplate_clause(clause: str) -> bool:
    """Detect legacy seed/template meta-prefixes that are not genuine persona impressions."""
    c = str(clause or "").strip()
    if not c:
        return False
    return bool(BOILERPLATE_RE.match(c))


def clause_salience(clause: str, index: int, total: int, *, age_days: float = 0.0) -> tuple[str, float]:
    """Score a clause into 'boilerplate', 'core' (>= 3.0), or 'ephemeral' (0.5 .. 1.8)."""
    if is_boilerplate_clause(clause):
        return ("boilerplate", 0.0)

    clean_clause = re.sub(r"[^\w]", "", clause)
    starts_ephemeral = any(clause.strip().startswith(prefix) for prefix in EPHEMERAL_TIME_START)
    matched_core = [s for s in ALL_CORE_SIGNALS if s in clause]

    # First clause characterizes persona if >= 8 chars and not starting with transient time markers
    is_first_anchor = (index == 0 and len(clean_clause) >= 8 and not starts_ephemeral)

    if is_first_anchor or matched_core:
        score = 3.5 if is_first_anchor else 3.0
        score += min(1.5, 0.4 * len(matched_core))
        # Core clauses do NOT decay with age
        return ("core", round(score, 3))

    matched_ephemeral = [m for m in EPHEMERAL_MARKERS if m in clause]
    base = 1.0 if (starts_ephemeral or matched_ephemeral) else 1.4
    recency = 0.3 * (index / max(1, total - 1)) if total > 1 else 0.0
    score = min(1.8, max(0.5, base + recency))

    # Ephemeral decay for older items (> 3.0 days)
    if age_days > 3.0:
        decay = 0.15 * min(float(age_days), 14.0)
        score = max(0.1, score - decay)

    return ("ephemeral", round(score, 3))


def _bigrams(s: str) -> set[str]:
    c = re.sub(r"[^\w]", "", s)
    if len(c) < 2:
        return {c} if c else set()
    return {c[i:i + 2] for i in range(len(c) - 1)}


def _containment_overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def dedupe_clauses(clauses: list[str], similarity_threshold: float = 0.58) -> list[str]:
    """Drop duplicate/containment redundant clauses, keeping the more informative/newer one."""
    if len(clauses) <= 1:
        return list(clauses)

    bigram_list = [_bigrams(c) for c in clauses]
    dropped = set()
    n = len(clauses)

    for i in range(n):
        if i in dropped:
            continue
        for j in range(i + 1, n):
            if j in dropped:
                continue
            overlap = _containment_overlap(bigram_list[i], bigram_list[j])
            if overlap >= similarity_threshold:
                len_i = len(re.sub(r"[^\w]", "", clauses[i]))
                len_j = len(re.sub(r"[^\w]", "", clauses[j]))
                if len_i > len_j + 3:
                    # i is significantly more informative
                    dropped.add(j)
                else:
                    # j is newer or longer
                    dropped.add(i)
                    break

    return [c for idx, c in enumerate(clauses) if idx not in dropped]


def _normalize_clause_punct(c: str) -> str:
    c = c.strip()
    if not c:
        return ""
    # Clean trailing repeated periods/semicolons
    c = re.sub(r"[。]+$", "。", c)
    c = re.sub(r"[；;]+$", "；", c)
    if c.endswith(("。", "！", "？", "!", "?", "；", ";", "…", "~")):
        return c
    return c + "。"


def compact_impression_text(text: str, *, extra_text: str = "", max_chars: int = 360,
                            keep_recent: int = 2, age_days: float = 0.0) -> str:
    """Compact impression text preserving core anchors and recent observations.

    - Strips legacy boilerplate seed prefixes.
    - Preserves core anchor traits and identity background.
    - Decays and forgets middle ephemeral trivialities when exceeding budget or stale.
    - Guarantees non-empty output for non-empty input.
    """
    text = str(text or "").rstrip()
    extra = str(extra_text or "").strip()

    if not text and not extra:
        return ""

    if extra and extra in text:
        extra = ""

    # Check for legacy boilerplate in original text
    all_orig_clauses = split_clauses(text)
    has_boilerplate = any(is_boilerplate_clause(c) for c in all_orig_clauses)

    if extra:
        sep = "" if (text and text[-1] in "。！？!?…~；;\n") else "。"
        candidate_concat = text + sep + extra if text else extra
    else:
        candidate_concat = text

    # Fast path: within character budget, no boilerplate, fresh enough
    if not has_boilerplate and len(candidate_concat) <= max_chars:
        if age_days < 5.0 or len(candidate_concat) <= 220:
            return candidate_concat

    # Split combined clauses
    if extra:
        sep = "" if (text and text[-1] in "。！？!?…~；;\n") else "。"
        combined_text = text + sep + extra if text else extra
    else:
        combined_text = text

    clauses = split_clauses(combined_text)
    if not clauses:
        return candidate_concat

    # Filter out boilerplate clauses
    cleaned_clauses = [c for c in clauses if not is_boilerplate_clause(c)]
    if not cleaned_clauses:
        # Safety: avoid emptying completely if input was only boilerplate
        cleaned_clauses = [clauses[-1]]

    # Deduplicate
    cleaned_clauses = dedupe_clauses(cleaned_clauses)
    if not cleaned_clauses:
        cleaned_clauses = [clauses[-1]]

    # Check if compaction is needed
    total_len = sum(len(_normalize_clause_punct(c)) for c in cleaned_clauses)
    needs_compaction = (total_len > max_chars) or (age_days >= 5.0 and total_len > 220)

    if not needs_compaction:
        return "".join(_normalize_clause_punct(c) for c in cleaned_clauses)

    # Compaction is needed
    num_clauses = len(cleaned_clauses)
    recent_k = min(keep_recent, num_clauses)

    if num_clauses <= recent_k:
        # All clauses are in the recent window
        tail_clauses = cleaned_clauses
        candidate_clauses = []
    else:
        tail_clauses = cleaned_clauses[-recent_k:]
        candidate_clauses = cleaned_clauses[:-recent_k]

    tail_chars = sum(len(_normalize_clause_punct(c)) for c in tail_clauses)

    # Score candidates
    scored_candidates = []
    for idx, c in enumerate(candidate_clauses):
        tier, score = clause_salience(c, idx, num_clauses, age_days=age_days)
        clause_len = len(_normalize_clause_punct(c))
        scored_candidates.append((idx, c, tier, score, clause_len))

    core_candidates = [item for item in scored_candidates if item[2] == "core"]
    ephemeral_candidates = [item for item in scored_candidates if item[2] == "ephemeral"]

    # 1. Prioritize core clauses in original order
    budget_for_candidates = max_chars - tail_chars
    selected_core = []
    used_core_len = 0
    for item in core_candidates:
        if used_core_len + item[4] <= budget_for_candidates:
            selected_core.append(item)
            used_core_len += item[4]

    # 2. Allocate remaining budget for ephemeral items
    target_budget = (220 if (age_days >= 5.0 and total_len > 220) else max_chars)
    ephemeral_budget = max(0, min(target_budget - tail_chars - used_core_len,
                                 max_chars - tail_chars - used_core_len))

    # Prefer higher score, tie-breaking by newer index
    sorted_ephemeral = sorted(ephemeral_candidates, key=lambda item: (item[3], item[0]), reverse=True)
    selected_ephemeral = []
    used_eph_len = 0
    for item in sorted_ephemeral:
        # If stale (age_days >= 5.0) and score decayed significantly (<= 0.3), drop
        if age_days >= 5.0 and item[3] <= 0.3 and total_len > 220:
            continue
        if used_eph_len + item[4] <= ephemeral_budget:
            selected_ephemeral.append(item)
            used_eph_len += item[4]

    # Combine candidates in original order
    combined_candidates = selected_core + selected_ephemeral
    combined_candidates.sort(key=lambda item: item[0])

    final_clauses = [item[1] for item in combined_candidates] + tail_clauses
    if not final_clauses:
        final_clauses = [cleaned_clauses[-1]]

    result = "".join(_normalize_clause_punct(c) for c in final_clauses)
    return result if result else candidate_concat
