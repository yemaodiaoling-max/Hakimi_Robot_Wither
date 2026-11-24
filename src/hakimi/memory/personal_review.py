"""Bounded 2-hour personal memory reflection scheduling.

This module never calls a model and never writes QQ output.  It selects at most
one public group and at most three well-evidenced people from the last 48 hours,
then publishes a trusted internal turn to the existing coordinator.  The idle
Gemini agent claims that turn, may call memory.social (exact original text) and
must finish with reply.skip.  qq_cli and TurnCoordinator independently block
reply/file/math/tool writes for this kind of turn.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from hakimi.memory.review_state import COOLDOWN, MAX_AUDIT_BYTES, MAX_AUDIT_LINES, MAX_EVIDENCE, MAX_EXCERPT, MAX_NEW_EVIDENCE, MAX_OLD_CONTEXT, MAX_PEOPLE, MAX_RETRIES_SAME_EVIDENCE_DAY, MAX_SEEN_EVIDENCE, MAX_STATE_BYTES, PERSON, PLAN_REFRESH_SECONDS, PUBLICATION_HISTORY_LIMIT, RECENT_WINDOW, RETRY_BACKOFF_SECONDS, REVIEW_INTERVAL, REVIEW_TTL_SECONDS, ReviewError, SIGNIFICANT, SIGNIFICANT_GAP, STATUSES, _audit, _audit_path, _day, _ensure_day, _evidence_signature, _load, _locked, _path, _publications, _save  # noqa: F401 (re-export)
from hakimi.memory.review_select import SOURCE_LABELS, _config_self_ids, _direct_events, _global_gemini_impressions, _latest_impressions, _manual_people, _note_selection, _recent_records, _row_reply, _row_targets, _self_origins, _source_label, _targets, _weighted_sample, select_review  # noqa: F401 (re-export)


def _turn_snapshot(base):
    try:
        from hakimi.bridge.turn_coordinator import TurnCoordinator
        return TurnCoordinator(str(Path(base).resolve())).snapshot()
    except Exception:
        return {}


def _backlog(base):
    root = Path(base) / 'state'
    snapshot = _turn_snapshot(base)
    if snapshot.get('status') not in {'idle','completed',None}:
        return 'turn_active'
    marker = root / 'pending_trigger.json'
    if marker.exists():
        try:
            payload = json.loads(marker.read_text(encoding='utf-8'))
            known = set(snapshot.get('recent_trigger_ids') or [])
            if snapshot.get('trigger_id'):
                known.add(snapshot['trigger_id'])
            if not isinstance(payload,dict) or payload.get('trigger_id') not in known:
                return 'pending_real_trigger'
        except (OSError,ValueError):
            return 'pending_real_trigger'

    try:
        delivery = json.loads((root / "delivery_state.json").read_text(encoding="utf-8"))
        pending = delivery.get("pending") if isinstance(delivery, dict) else None
        if isinstance(pending, dict) and pending:
            # Delivery of an already-generated reply runs in the worker, not
            # the model. A silent review can use that idle model slot without
            # changing or delaying the pending reply. Unknown/unrelated queue
            # entries still defer, as does every unprocessed real trigger above.
            known = set(snapshot.get('recent_trigger_ids') or [])
            if snapshot.get('trigger_id'):
                known.add(snapshot['trigger_id'])
            generated_reply = (snapshot.get('status') == 'completed'
                and str(pending.get('trigger_id') or '') in known)
            if not generated_reply:
                return "delivery_backlog"
    except (OSError, ValueError):
        pass
    snapshot = _turn_snapshot(base)
    if snapshot.get("status") not in {"idle", "completed", None}:
        return "turn_active"
    return ""


def _mark(base, review_id, status, now, reason="", count=0):
    state = _load(base)
    pending = state.get("pending") or {}
    if pending.get("review_id") != review_id:
        return False
    state["pending"]["status"] = status
    state["pending"]["updated_at"] = now
    review = state["reviews"].setdefault(review_id, {"review_id": review_id, "at": pending.get("published_at", now),
                                                      "group": pending.get("group", ""), "written": []})
    review["status"] = status
    review["at"] = float(review.get("at", now))
    review["updated_at"] = now
    if reason:
        review["reason"] = reason[:120]
    _audit(base, review, status, now, reason, count)
    daily = _ensure_day(state, now)
    people_evidence = pending.get("people_evidence") if isinstance(pending.get("people_evidence"), dict) else {}
    if status in {"no_change", "written"}:
        # A completed successful review becomes the new seen anchor.  Only the
        # exact evidence offered is marked seen; no non-direct chat is imported.
        for person in pending.get("people", []):
            row = state["people"].setdefault(person, {})
            row["last_reviewed"] = now
            row["last_success_at"] = now
            info = people_evidence.get(person) if isinstance(people_evidence.get(person), dict) else {}
            seen = [str(value) for value in row.get("seen_evidence_ids", [])]
            for value in info.get("evidence_ids", []):
                text = str(value).upper().removeprefix("M")
                if text.isdigit() and "M" + text not in seen:
                    seen.append("M" + text)
            row["seen_evidence_ids"] = seen[-MAX_SEEN_EVIDENCE:]
            row["retry"] = {}
            daily["no_change" if status == "no_change" else "written"] += 1
        group = str(pending.get("group", ""))
        if group:
            state["groups"].setdefault(group, {})["last_reviewed"] = now
        _audit(base, review, "completed", now, reason, count)
    elif status == "expired":
        # Failure/expiry releases the pending turn without a review cooldown and
        # without discarding evidence.  Retry the same evidence at most twice a
        # day with a >=30-minute backoff; newer evidence starts a new signature.
        day = _day(now)
        for person in pending.get("people", []):
            info = people_evidence.get(person) if isinstance(people_evidence.get(person), dict) else {}
            signature = str(info.get("signature", "")) or _evidence_signature(info.get("evidence_ids", []))
            row = state["people"].setdefault(person, {})
            retry = row.get("retry") if isinstance(row.get("retry"), dict) else {}
            attempts = int(retry.get("attempts", 0) or 0) if retry.get("signature") == signature and retry.get("day") == day else 0
            attempts += 1
            row["retry"] = {
                "signature": signature, "day": day, "attempts": attempts,
                "retry_after": now + RETRY_BACKOFF_SECONDS * max(1, attempts),
                "reason": str(reason or status)[:80],
            }
        daily["expired"] += 1
        # A failed/expired turn may retry the same new evidence after the
        # bounded backoff, not only at the normal two-hour publication slot.
        state["next_due"] = now + RETRY_BACKOFF_SECONDS
        # last_reviewed/seen_evidence_ids are deliberately untouched.
    if status in {"completed", "no_change", "written", "expired"}:
        state["pending"] = None
    _save(base, state)
    return True


def mark_claimed(base, review_id, trigger_id=""):
    """Called by the trusted prompt renderer when the coordinator claims a turn."""
    now = time.time()
    with _locked(base):
        state = _load(base)
        pending = state.get("pending") or {}
        if (pending.get("review_id") != str(review_id) or pending.get("status") == "claimed"):
            return False
        if trigger_id and pending.get("trigger_id") not in (None, "", str(trigger_id)):
            return False
        return _mark(base, review_id, "claimed", now)


def mark_written(base, review_id, target_id, receipt=None, now=None):
    """Record a successful memory.social receipt; exact text stays in memory_store."""
    now = time.time() if now is None else float(now)
    with _locked(base):
        state = _load(base)
        pending = state.get("pending") or {}
        if pending.get("review_id") != str(review_id):
            return False
        target = str(target_id)
        if target not in pending.get("people", []):
            return False
        state["pending"].setdefault("written", [])
        if target in state["pending"]["written"]:
            return True
        state["pending"]["written"].append(target)
        state["people"].setdefault(target, {})["last_written"] = now
        person_row = state["people"].setdefault(target, {})
        person_row["last_success_at"] = now
        info = (pending.get("people_evidence") or {}).get(target) if isinstance(
            pending.get("people_evidence"), dict) else {}
        if isinstance(info, dict):
            seen = [str(value) for value in person_row.get("seen_evidence_ids", [])]
            for value in info.get("evidence_ids", []):
                text = str(value).upper().removeprefix("M")
                if text.isdigit() and "M" + text not in seen:
                    seen.append("M" + text)
            person_row["seen_evidence_ids"] = seen[-MAX_SEEN_EVIDENCE:]
            person_row["retry"] = {}
        group = str(pending.get("group", ""))
        if group:
            state["groups"].setdefault(group, {})["last_written"] = now
        review = state["reviews"].setdefault(pending["review_id"], {
            "review_id": pending["review_id"], "at": pending.get("published_at", now),
            "group": group, "written": []})
        review.setdefault("written", []).append(target)
        review["status"] = "written"
        review["updated_at"] = now
        _audit(base, review, "written", now, count=len(review["written"]))
        _save(base, state)
        return True


def mark_completed(base, review_id, now=None, reason="no_change"):
    now = time.time() if now is None else float(now)
    with _locked(base):
        state = _load(base)
        pending = state.get("pending") or {}
        if pending.get("review_id") != str(review_id):
            return False
        status = "written" if pending.get("written") else "no_change"
        return _mark(base, review_id, status, now, reason, len(pending.get("written", [])))


def reconcile(base, now=None):
    now = time.time() if now is None else float(now)
    with _locked(base):
        state = _load(base)
        pending = state.get("pending") or {}
        if not pending:
            return {"status": "idle"}
        trigger = str(pending.get("trigger_id", ""))
        snapshot = _turn_snapshot(base)
        status = snapshot.get("status")
        if status in {"queued", "primary_active", "fallback_active"} and str(
                snapshot.get("trigger_id", "")) == trigger:
            if now - float(pending.get("published_at", now)) >= REVIEW_TTL_SECONDS:
                _mark(base, pending["review_id"], "expired", now, "review_timeout")
                return {"status": "expired", "trigger_id": trigger}
            if pending.get("status") == "scheduled":
                if snapshot.get("status") == "primary_active":
                    _mark(base, pending["review_id"], "claimed", now)
                else:
                    _save(base, state)
            return {"status": pending.get("status", "scheduled"), "trigger_id": trigger}
        if status == "completed" and str(snapshot.get("trigger_id", "")) == trigger:
            outcome = str(snapshot.get("outcome", ""))
            if outcome in {"expired", "review_failed", "failed", "error"}:
                result = "expired"
            else:
                result = "written" if pending.get("written") else "no_change"
            _mark(base, pending["review_id"], result, now, "coordinator_" + (outcome or result),
                  len(pending.get("written", [])))
            return {"status": result, "trigger_id": trigger}
        # The turn is gone, was superseded, or was taken by an unguarded path.
        # Never send anything after this point.
        if now - float(pending.get("published_at", now)) >= REVIEW_TTL_SECONDS:
            _mark(base, pending["review_id"], "expired", now, "turn_not_completed")
            return {"status": "expired", "trigger_id": trigger}
        _save(base, state)
        return {"status": pending.get("status", "scheduled"), "trigger_id": trigger}


def _publish(base, review, now):
    from hakimi.bridge.turn_coordinator import TurnCoordinator
    payload = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "wake_mode": "个人记忆反思",
        "session_key": review["group"],
        "message_type": "group",
        "recent_context_messages": [],
        "reply_target_message_id": "",
        "internal_review": review,
        "is_proactive": False,
    }
    trigger_id = "personal-" + str(review["review_id"])
    payload["trigger_id"] = trigger_id
    result = TurnCoordinator(str(Path(base).resolve())).publish(payload)
    return result, trigger_id


def _vertex_budget_blocked(base, now):
    """Only the Gemini-only reflection is gated; Terra work is unaffected."""
    try:
        from hakimi.models import vertex_route
        route = vertex_route.load(base)
        if not route.enabled:
            return False
        return bool(vertex_route.check(base, route=route, now=now).get("blocked"))
    except Exception:
        # A trusted switch that cannot be verified is never assumed unlimited.
        return True


def tick(base, now=None, publish=True):
    """Run one 5-minute supervisor tick without ever creating a QQ reply."""
    now = time.time() if now is None else float(now)
    with _locked(base):
        state = _load(base)
        daily = _ensure_day(state, now)
        publications = _publications(state, now)
        pending = state.get("pending") or {}
        if pending:
            return {"status": pending.get("status", "scheduled"),
                    "review_id": pending.get("review_id"),
                    "next_due": state.get("next_due", 0),
                    "budget_model": False}
        # Release only the former daily-count delay; keep the ordinary interval.
        if state.get('last_result', {}).get('status') == 'daily_quota_reached':
            state['next_due'] = max(publications, default=now-REVIEW_INTERVAL) + REVIEW_INTERVAL
            state['last_result'] = {'at':now,'status':'daily_cap_removed'}
            _save(base,state)
        plan = state.get("pending_plan")
        if plan and now - float(plan.get("created_at", 0) or 0) >= PLAN_REFRESH_SECONDS:
            # Never publish a stale selection: re-read current evidence first.
            fresh = select_review(base, now, state=state)
            plan = fresh
            state["pending_plan"] = fresh
            if not fresh:
                state["next_due"] = now + REVIEW_INTERVAL
                state["last_result"] = {"at": now, "status": "no_candidates",
                                        "reason": "plan_refreshed_no_new_evidence"}
                daily["no_candidates"] += 1
                _save(base, state)
                return {"status": "no_candidates", "next_due": state["next_due"],
                        "review_id": None, "budget_model": False}
            _note_selection(state, plan, now)
            _save(base, state)
        if not plan and now < float(state.get("next_due", 0) or 0):
            return {"status": "not_due", "next_due": state.get("next_due", 0),
                    "review_id": None, "budget_model": False}
        if not plan:
            daily["attempts"] += 1
            plan = select_review(base, now, state=state)
            if not plan:
                state["next_due"] = now + REVIEW_INTERVAL
                state["pending_plan"] = None
                state["last_result"] = {"at": now, "status": "no_candidates"}
                daily["no_candidates"] += 1
                _save(base, state)
                return {"status": "no_candidates", "next_due": state["next_due"],
                        "review_id": None, "budget_model": False}
            state["pending_plan"] = plan
            _note_selection(state, plan, now)
            _save(base, state)
        if not publish:
            return {"status": "selected", "next_due": now + REVIEW_INTERVAL,
                    "review": plan, "budget_model": True}
        if _vertex_budget_blocked(base, now):
            state["last_result"] = {"at": now, "status": "skipped_vertex_budget",
                                    "review_id": plan.get("review_id")}
            state["next_due"] = now + 300
            daily["deferred"] += 1
            _save(base, state)
            return {"status": "skipped_vertex_budget", "review_id": plan.get("review_id"),
                    "next_due": state["next_due"], "budget_model": False}
        backlog = _backlog(base)
        if backlog:
            state["last_result"] = {"at": now, "status": "deferred", "reason": backlog}
            daily["deferred"] += 1
            _save(base, state)
            return {"status": "deferred", "reason": backlog,
                    "next_due": state.get("next_due", now + REVIEW_INTERVAL),
                    "review_id": plan.get("review_id"), "budget_model": False}
        result, trigger_id = _publish(base, plan, now)
        if not result.get("accepted"):
            state["last_result"] = {"at": now, "status": "deferred",
                                    "reason": str(result.get("reason", "busy"))[:80]}
            daily["deferred"] += 1
            _save(base, state)
            return {"status": "deferred", "reason": str(result.get("reason", "busy")),
                    "next_due": state.get("next_due", now + REVIEW_INTERVAL),
                    "review_id": plan.get("review_id"), "budget_model": False}
        for candidate in plan.get("candidates", []):
            if float(candidate.get("significant_at", 0) or 0):
                state["people"].setdefault(candidate["person_id"], {})["last_significant_at"] = float(
                    candidate["significant_at"])
        publications.append(now)
        state["publications"] = sorted(publications)[-PUBLICATION_HISTORY_LIMIT:]
        daily["published"] += 1
        state["pending_plan"] = None
        people_evidence = {}
        for item in plan.get("candidates", []):
            people_evidence[item["person_id"]] = {
                "signature": item.get("evidence_signature", ""),
                "evidence_ids": item.get("evidence_memory_ids", []),
            }
        state["pending"] = {
            "review_id": plan["review_id"], "trigger_id": trigger_id,
            "group": plan["group"], "people": [item["person_id"] for item in plan["candidates"]],
            "people_evidence": people_evidence,
            "published_at": now, "status": "scheduled", "written": [],
        }
        state["reviews"][plan["review_id"]] = {
            "review_id": plan["review_id"], "at": now, "group": plan["group"],
            "status": "scheduled", "written": [], "candidate_count": len(plan["candidates"]),
        }
        state["last_result"] = {"at": now, "status": "scheduled", "review_id": plan["review_id"],
                                "published_24h": len(publications)}
        # The next slot is two hours from actual publication, never from an old
        # selection time.
        state["next_due"] = now + REVIEW_INTERVAL
        _audit(base, plan, "scheduled", now, count=len(plan["candidates"]))
        _save(base, state)
        return {"status": "scheduled", "review_id": plan["review_id"], "trigger_id": trigger_id,
                "next_due": state["next_due"], "budget_model": True}


def report(base, now=None):
    """Counts only: no messages, prompts, tokens or credentials."""
    now = time.time() if now is None else float(now)
    state = _load(base)
    counts = {key: 0 for key in STATUSES}
    for item in state.get("reviews", {}).values():
        status = str((item or {}).get("status", ""))
        if status in counts:
            counts[status] += 1
    pending = state.get("pending") or {}
    publications = _publications(state, now)
    daily = _ensure_day(state, now)
    return {
        "next_due": state.get("next_due", 0),
        "due": now >= float(state.get("next_due", 0) or 0),
        "pending": bool(pending),
        "pending_status": pending.get("status"),
        "last_result": state.get("last_result"),
        "reviews": counts,
        "people_tracked": len(state.get("people", {})),
        "groups_tracked": len(state.get("groups", {})),
        "last_reviewed": max((float((row or {}).get("last_reviewed", 0) or 0)
                              for row in state.get("people", {}).values()), default=0),
        "last_written": max((float((row or {}).get("last_written", 0) or 0)
                             for row in state.get("people", {}).values()), default=0),
        "published_24h": len(publications),
        "published_quota_24h": None,
        "daily": dict(day=daily.get("day", ""), **{key: int(daily.get(key, 0) or 0) for key in
                  ("attempts", "published", "no_candidates", "deferred",
                   "expired", "no_change", "written")}),
        "new_counts": state.get("last_selection") or {},
    }
