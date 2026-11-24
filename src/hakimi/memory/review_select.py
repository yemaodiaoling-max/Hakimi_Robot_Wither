"""Choose up to three people worth a personal memory review, with bounded evidence."""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import sqlite3
import time
from pathlib import Path

from hakimi.memory.memory_lifecycle import impression_excerpt
from hakimi.memory.review_state import COOLDOWN, MAX_EVIDENCE, MAX_EXCERPT, MAX_NEW_EVIDENCE, MAX_OLD_CONTEXT, MAX_PEOPLE, MAX_RETRIES_SAME_EVIDENCE_DAY, PERSON, RECENT_WINDOW, RETRY_BACKOFF_SECONDS, REVIEW_INTERVAL, SIGNIFICANT, SIGNIFICANT_GAP, _day, _evidence_signature, _load


def _note_selection(state, plan, now):
    state["last_selection"] = {
        "at": now,
        "review_id": (plan or {}).get("review_id"),
        "people": len((plan or {}).get("candidates", [])),
        "new_evidence_rows": sum(len(item.get("new_evidence_ids", []))
                                 for item in (plan or {}).get("candidates", [])),
        "new_direct_events": sum(int(item.get("new_direct_count", 0) or 0)
                                 for item in (plan or {}).get("candidates", [])),
        "context_only_people": sum(1 for item in (plan or {}).get("candidates", [])
                                   if not item.get("new_direct_count")),
        "novelty": round(sum(float(item.get("novelty", 0) or 0)
                             for item in (plan or {}).get("candidates", [])), 3),
    }


def _global_gemini_impressions(db):
    """Latest genuine Gemini write per target across public groups.

    Terra/DSH auxiliary rows never become the seen anchor.  A genuine write in
    another public group therefore suppresses re-review of the same older
    material here until this group has genuinely new evidence.
    """
    result = {}
    try:
        rows = db.execute(
            "SELECT id,target_id,impression,created_at,affinity,closeness,confidence FROM social_assessments "
            "WHERE state='active' AND subject_id='hakimi' AND provenance='gemini_manual' "
            "AND session LIKE 'group:%' ORDER BY created_at DESC,id DESC",
        ).fetchall()
    except sqlite3.Error:
        return result
    for row in rows:
        key = str(row["target_id"])
        if key not in result:
            result[key] = {"impression": impression_excerpt(row["impression"]),
                           "created_at": float(row["created_at"] or 0),
                           "affinity": row["affinity"],
                           "closeness": row["closeness"],
                           "confidence": row["confidence"],
                           "assessment_id": int(row["id"])}
    return result


def _config_self_ids(base):
    try:
        cfg = json.loads((Path(base) / "config.json").read_text(encoding="utf-8"))
        value = str((cfg.get("bot_profile") or {}).get("self_qq", ""))
        return {value} if PERSON.fullmatch(value) else set()
    except (OSError, ValueError, AttributeError):
        return set()


def _targets(value):
    if isinstance(value, (list, tuple)):
        return {str(item) for item in value if PERSON.fullmatch(str(item))}
    if not isinstance(value, str) or not value.strip():
        return set()
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return {str(item) for item in parsed if PERSON.fullmatch(str(item))}
    except (TypeError, ValueError):
        pass
    return {item for item in re.findall(r"[1-9][0-9]{0,19}", value)}


def _recent_records(db, since):
    rows = db.execute(
        "SELECT id,origin,user_id,session,at,text,direct_targets,reply_to,self_id,nickname "
        "FROM records WHERE kind='chat' AND state!='deleted' AND session LIKE 'group:%' "
        "AND at>=? ORDER BY at DESC,id DESC LIMIT 20000", (float(since),),
    ).fetchall()
    return [dict(row) for row in rows]


def _row_targets(row):
    """Read current-speaker @ targets, including old rows with CQ in text."""
    targets = _targets(row.get("direct_targets"))
    if targets:
        return targets
    try:
        from hakimi.social.social_base import _text_targets
        return set(_text_targets(str(row.get("text") or "")))
    except (ImportError, TypeError, ValueError):
        return set()


def _row_reply(row):
    """Read the reply reference, falling back to a legacy CQ reply code."""
    reply = str(row.get("reply_to") or "")
    if reply:
        return reply
    try:
        from hakimi.social.social_base import _reply_target
        return str(_reply_target(str(row.get("text") or "")) or "")
    except (ImportError, TypeError, ValueError):
        return ""


def _direct_events(rows, self_ids, self_origins=()):
    # QQ reply ids are record.origin values, not SQLite row ids.
    by_origin = {}
    for row in rows:
        origin = str(row.get("origin") or "")
        if origin:
            by_origin.setdefault((str(row["session"]), origin), row)
    self_origins = {str(value) for value in self_origins if value}
    # replies may point to a message just outside the 48h window; query remains bounded.
    events = {}
    for row in rows:
        author = str(row["user_id"] or "")
        if not PERSON.fullmatch(author) or author in self_ids:
            continue
        refs = _row_targets(row)
        reply = _row_reply(row)
        if reply and reply in self_origins:
            refs.add("__self__")
        if reply:
            target = by_origin.get((str(row["session"]), reply))
            if target:
                refs.add(str(target["user_id"]))
        if "__self__" in refs or refs & self_ids:
            events.setdefault((str(row["session"]), author), []).append(row)
    return events


def _self_origins(base):
    result = set()
    try:
        with (Path(base) / "state" / "sent_messages.jsonl").open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            start = max(0, stream.tell() - 512 * 1024)
            stream.seek(start)
            if start:
                stream.readline()
            raw = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return result
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("message_id"):
            result.add(str(row["message_id"]))
    return result


SOURCE_LABELS = {
    "gemini_manual": "Gemini 亲笔",
    "dsh_assisted": "DSH 辅助整理",
    "terra_assisted": "Terra 观察（辅助，非当事人内心）",
}


def _source_label(provenance):
    return SOURCE_LABELS.get(str(provenance or ""), str(provenance or "未知来源"))


def _latest_impressions(db):
    """Latest *Hakimi-authored* impression per (session, target).

    Terra's A->B observation has an explicit subject_id, so it must never be
    mistaken for Hakimi's own impression of B.  Auxiliary write times are
    exposed separately from the latest genuine Gemini write time.
    """
    result = {}
    try:
        rows = db.execute(
            "SELECT id,session,target_id,subject_id,impression,created_at,provenance,affinity,closeness,confidence "
            "FROM social_assessments WHERE state='active' AND subject_id='hakimi' "
            "ORDER BY created_at DESC,id DESC",
        ).fetchall()
    except sqlite3.Error:
        return result
    for row in rows:
        key = (str(row["session"]), str(row["target_id"]))
        provenance = str(row["provenance"] or "")
        item = result.get(key)
        if item is None:
            item = {"impression": impression_excerpt(row["impression"]),
                    "created_at": float(row["created_at"] or 0),
                    "provenance": provenance,
                    "source": _source_label(provenance),
                    "gemini_created_at": 0.0,
                    "affinity": row["affinity"] if provenance in {"gemini_manual", "dsh_assisted"} else None,
                    "closeness": row["closeness"] if provenance in {"gemini_manual", "dsh_assisted"} else None,
                    "confidence": row["confidence"] if provenance in {"gemini_manual", "dsh_assisted"} else None,
                    "assessment_id": int(row["id"]) if provenance in {"gemini_manual", "dsh_assisted"} else None}
            result[key] = item
        if provenance == "gemini_manual" and not item["gemini_created_at"]:
            item["gemini_created_at"] = float(row["created_at"] or 0)
            for field in ("affinity", "closeness", "confidence"):
                if row[field] is not None:
                    item[field] = row[field]
            item["assessment_id"] = int(row["id"])
    return result


def _manual_people(db):
    try:
        return {str(row[0]) for row in db.execute(
            "SELECT DISTINCT user_id FROM records WHERE kind='person' AND state!='deleted' "
            "AND user_id GLOB '[1-9]*'")}
    except sqlite3.Error:
        return set()


def _weighted_sample(items, weights, count, rng, explore=0.2):
    """Mixed 80% weighted / 20% exploration without replacement."""
    remaining = list(range(len(items)))
    chosen = []
    while remaining and len(chosen) < count:
        if len(items) > 1 and rng.random() < explore:
            index = remaining[rng.randrange(len(remaining))]
        else:
            total = sum(max(0.0001, weights[i]) for i in remaining)
            point = rng.random() * total
            index = remaining[-1]
            for candidate in remaining:
                point -= max(0.0001, weights[candidate])
                if point <= 0:
                    index = candidate
                    break
        chosen.append(items[index])
        remaining.remove(index)
    return chosen


def select_review(base, now=None, *, limit=MAX_PEOPLE, rng=None, state=None):
    """Select <=1 group and <=3 people using genuinely new real evidence.

    Evidence older than the latest genuine Gemini write or the last successful
    review is context only and can never trigger a model call by age alone.
    """
    now = time.time() if now is None else float(now)
    state = state or _load(base)
    rng = rng or random.Random(int(now // REVIEW_INTERVAL) ^ 0x5A17)
    from hakimi.memory.memory_store import store
    index = store(str(Path(base).resolve()))
    with index.db() as db:
        rows = _recent_records(db, now - RECENT_WINDOW)
        self_ids = _config_self_ids(base) | {str(row["self_id"]) for row in rows
                                             if PERSON.fullmatch(str(row["self_id"] or ""))}
        events = _direct_events(rows, self_ids, _self_origins(base))
        impressions = _latest_impressions(db)
        global_impressions = _global_gemini_impressions(db)
        manual = _manual_people(db)
    groups = {}
    people = {}
    for row in rows:
        author = str(row["user_id"] or "")
        if PERSON.fullmatch(author) and author not in self_ids:
            people.setdefault((str(row["session"]), author), []).append(row)
    for (session, person), own in people.items():
        direct = events.get((session, person), [])
        # Newest evidence first; direct @/reply rows remain the primary record.
        own.sort(key=lambda row: (float(row["at"] or 0), int(row["id"] or 0)), reverse=True)
        direct_ids = {int(row["id"]) for row in direct}
        impression = impressions.get((session, person)) or {}
        global_prior = global_impressions.get(person) or {}
        last_write = float(impression.get("created_at", 0) or 0)
        gemini_write = max(float(impression.get("gemini_created_at", 0) or 0),
                           float(global_prior.get("created_at", 0) or 0))
        person_state = state.get("people", {}).get(person) or {}
        last_success = max(float(person_state.get("last_success_at", 0) or 0),
                           float(person_state.get("last_reviewed", 0) or 0))
        last_reviewed = float(person_state.get("last_reviewed", 0) or 0)
        anchor = max(gemini_write, last_success)
        seen = set()
        for value in person_state.get("seen_evidence_ids", []):
            text = str(value).upper().removeprefix("M")
            if text.isdigit():
                seen.add(int(text))
        new_rows = [row for row in own
                    if float(row["at"] or 0) > anchor and int(row["id"]) not in seen]
        if not new_rows:
            continue
        new_direct = [row for row in new_rows if int(row["id"]) in direct_ids]
        new_context = [row for row in new_rows if int(row["id"]) not in direct_ids]
        # One real direct event is enough (unknown people included).  A person
        # with only public author context needs five genuinely new messages and
        # receives a lower weight.
        if not (new_direct or len(new_context) >= 5):
            continue
        previous_significant = float(person_state.get("last_significant_at", 0) or 0)
        new_significant = max((float(row["at"] or 0) for row in new_direct
                               if float(row["at"] or 0) > last_reviewed
                               and SIGNIFICANT.search(str(row.get("text") or ""))), default=0)
        marker = previous_significant or last_reviewed
        significant_at = (new_significant if new_significant
                          and new_significant >= marker + SIGNIFICANT_GAP else 0)
        new_ids = [int(row["id"]) for row in new_rows[:24]]
        retry_signature = _evidence_signature(new_ids)
        retry = person_state.get("retry") if isinstance(person_state.get("retry"), dict) else {}
        same_retry = retry.get("signature") == retry_signature
        attempts = int(retry.get("attempts", 0) or 0) if same_retry else 0
        retry_day = str(retry.get("day", ""))
        if same_retry and now < float(retry.get("retry_after", 0) or 0):
            continue
        retry_due = bool(same_retry and retry_day == _day(now)
                         and attempts < MAX_RETRIES_SAME_EVIDENCE_DAY
                         and now >= float(retry.get("retry_after", 0) or 0))
        if same_retry and retry_day == _day(now) and attempts >= MAX_RETRIES_SAME_EVIDENCE_DAY:
            continue
        if (last_success and now - last_success < COOLDOWN and not significant_at
                and not retry_due and len(new_direct) < 3):
            continue
        # New evidence is primary; at most two older direct rows remain context.
        new_sorted = sorted(new_rows, key=lambda row: (int(row["id"]) in direct_ids,
                                                       float(row["at"] or 0),
                                                       int(row["id"] or 0)), reverse=True)
        old_context = [row for row in own if int(row["id"]) not in {int(r["id"]) for r in new_sorted}]
        old_context.sort(key=lambda row: (int(row["id"]) in direct_ids,
                                          float(row["at"] or 0), int(row["id"] or 0)), reverse=True)
        chosen_rows = []
        for row in new_sorted[:MAX_NEW_EVIDENCE] + old_context[:MAX_OLD_CONTEXT]:
            if int(row["id"]) in {int(item["id"]) for item in chosen_rows}:
                continue
            text = str(row.get("text") or "").replace("\x00", "")
            if not text.strip():
                continue
            chosen_rows.append(row)
            if len(chosen_rows) >= MAX_EVIDENCE:
                break
        if not chosen_rows:
            continue
        evidence = [{"memory_id": "M" + str(row["id"]), "user_id": person,
                     "at": float(row["at"] or 0), "date": _day(row["at"]),
                     "excerpt": str(row.get("text") or "").replace("\x00", "")[:MAX_EXCERPT],
                     "direct": int(row["id"]) in direct_ids}
                    for row in chosen_rows]
        last_direct = max((float(row["at"] or 0) for row in (new_direct or new_rows)), default=0.0)
        days = sorted({_day(row["at"]) for row in new_direct if _day(row["at"])})
        from hakimi.memory.memory_lifecycle import impression_novelty
        novelty = impression_novelty(global_prior.get("impression", ""),
                                     [row["excerpt"] for row in evidence])
        unknown = 1.35 if not gemini_write else 1.0
        uncertainty = 1.18 if (not gemini_write or novelty >= 0.70) else 1.0
        manual_bonus = 1.2 if person in manual else 1.0
        # Saturating wait bonus is applied only because new evidence exists.
        wait_hours = max(0.0, (now - max(anchor, last_reviewed)) / 3600)
        wait_bonus = 1.0 + min(1.5, math.log1p(wait_hours) / math.log1p(72))
        interaction = sum(math.log1p(max(0, len(direct))) for direct in [new_direct, new_context])
        novelty_factor = 0.55 + 0.9 * max(0.0, min(1.0, novelty))
        context_factor = 1.0 if new_direct else 0.55
        weight = ((1.0 + 2.4 * len(new_direct) + 0.7 * len(new_context) + interaction)
                  * novelty_factor * unknown * uncertainty * manual_bonus * wait_bonus
                  * context_factor)
        if weight <= 0:
            continue
        groups.setdefault(session, []).append({
            "person_id": person,
            "weight": weight,
            "direct_events": len(new_direct),
            "new_direct_count": len(new_direct),
            "new_context_count": len(new_context),
            "new_evidence_ids": ["M" + str(row["id"]) for row in new_rows[:24]],
            "evidence_signature": retry_signature,
            "direct_days": days,
            "last_activity_at": last_direct,
            "last_impression": global_prior.get("impression") or impression.get("impression", ""),
            "last_write_at": last_write,
            "last_write_date": _day(last_write) if last_write else "",
            "last_write_provenance": impression.get("provenance", ""),
            "last_write_source": impression.get("source", ""),
            "last_gemini_write_at": gemini_write,
            "significant_at": significant_at,
            "novelty": round(novelty, 3),
            "unknown": not bool(gemini_write),
            "evidence": evidence,
        })
    if not groups:
        return None
    group_ids = []
    group_weights = []
    for session, candidates in groups.items():
        previous = float((state.get("groups", {}).get(session) or {}).get("last_reviewed", 0) or 0)
        age_days = min(7.0, max(0.0, now - previous) / 86400) if previous else 7.0
        fairness = 1.0 + age_days / 2
        group_ids.append(session)
        group_weights.append(sum(item["weight"] for item in candidates) * fairness)
    selected_group = _weighted_sample(group_ids, group_weights, 1, rng, explore=0.2)[0]
    candidates = groups[selected_group]
    chosen = _weighted_sample(candidates, [item["weight"] for item in candidates],
                              min(limit, MAX_PEOPLE), rng)
    chosen_ids = sorted(item["person_id"] for item in chosen)
    evidence_signature = _evidence_signature(
        [mid for item in chosen for mid in item["new_evidence_ids"]])
    review_id = "review-" + hashlib.sha256(
        (selected_group + "|" + ",".join(chosen_ids) + "|" + evidence_signature
         + "|" + str(int(now // REVIEW_INTERVAL))).encode()).hexdigest()[:20]
    candidates_out = []
    for item in chosen:
        candidates_out.append({
            "person_id": item["person_id"],
            "evidence": item["evidence"],
            "evidence_memory_ids": [row["memory_id"] for row in item["evidence"]],
            "new_evidence_ids": item["new_evidence_ids"],
            "evidence_signature": item["evidence_signature"],
            "direct_events": item["direct_events"],
            "new_direct_count": item["new_direct_count"],
            "new_context_count": item["new_context_count"],
            "direct_days": item["direct_days"],
            "novelty": item["novelty"],
            "unknown": item["unknown"],
            "last_activity_at": item["last_activity_at"],
            "last_impression": item["last_impression"],
            "last_write_at": item["last_write_at"],
            "last_write_date": item["last_write_date"],
            "last_write_provenance": item["last_write_provenance"],
            "last_write_source": item["last_write_source"],
            "last_gemini_write_at": item["last_gemini_write_at"],
            "significant_at": item["significant_at"],
        })
    from hakimi.social.social_behavior import behavior_for
    for candidate in candidates_out:
        own = behavior_for(base, candidate['person_id'], selected_group, now=now)
        raw_prior = global_impressions.get(candidate['person_id']) or impressions.get((selected_group, candidate['person_id'])) or {}
        same_prior = impressions.get((selected_group, candidate['person_id'])) or {}
        candidate['previous_scores'] = {
            'affinity': own['affinity'] if own['affinity'] is not None else (raw_prior.get('affinity') if raw_prior.get('affinity') is not None else same_prior.get('affinity')),
            'closeness': raw_prior.get('closeness') if raw_prior.get('closeness') is not None else same_prior.get('closeness'),
            'confidence': own['confidence'] if own['confidence'] is not None else (raw_prior.get('confidence') if raw_prior.get('confidence') is not None else same_prior.get('confidence')),
            'assessment_id': own['assessment_id'] or raw_prior.get('assessment_id') or same_prior.get('assessment_id'),
            'known': own['known'],
        }
    return {
        "review_id": review_id,
        "group": selected_group,
        "created_at": now,
        "next_due": now + REVIEW_INTERVAL,
        "selection": "weighted-80/20-new-evidence-no-replacement",
        "people_limit": MAX_PEOPLE,
        "candidates": candidates_out,
        "policy": {
            "scope": "one public group per review; private data never included",
            "evidence_limit": MAX_EVIDENCE,
            "new_evidence_limit": MAX_NEW_EVIDENCE,
            "old_context_limit": MAX_OLD_CONTEXT,
            "cooldown_seconds": COOLDOWN,
            "significant_gap_seconds": SIGNIFICANT_GAP,
            "max_published_24h": None,
            "retry_backoff_seconds": RETRY_BACKOFF_SECONDS,
            "scores": "optional; no forced positivity or intimate/sensitive inference",
        },
    }
