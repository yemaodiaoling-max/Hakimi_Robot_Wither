"""Host adapters for score projections; exact note text is never changed."""
import json
from hakimi.social.affinity_curve import adjust_affinity


def _clamp(value, low, high):
    return max(low, min(high, value))


def _tokens(text):
    try:
        from hakimi.memory.text import terms
        return set(terms(str(text or "")))
    except Exception:
        value = str(text or "")
        return {value[index:index + 2] for index in range(max(0, len(value) - 1))}


def apply_terra_curve(db, fields, now, evidence_rows):
    """Bounded, evidence-weighted Terra projection; Gemini curve stays untouched.

    The raw note in ``fields['note']`` is never modified.  Only the mutable
    social projection scores and this explainable policy metadata are changed.
    """
    from hakimi.social.social_base import TERRA_PROVENANCE, _utc_day
    public = str(fields["session"]).startswith("group:")
    scope = "session LIKE 'group:%'" if public else "session=?"
    params = [fields["subject_id"], fields["target_id"], TERRA_PROVENANCE]
    if not public:
        params.append(fields["session"])
    prior = db.execute(
        "SELECT * FROM social_assessments WHERE subject_id=? AND target_id=? "
        "AND provenance=? AND state='active' AND " + scope +
        " ORDER BY created_at DESC,id DESC LIMIT 1", params,
    ).fetchone()
    new_ids = {int(value) for value in fields.get("evidence", [])}
    prior_ids = set()
    if prior is not None:
        try:
            prior_ids = {int(value) for value in json.loads(prior["evidence_record_ids"] or "[]")}
        except (TypeError, ValueError):
            prior_ids = set()
    # Count actual, visible directional chat evidence; no caller-provided count.
    from hakimi.social.social_base import _evidence_involves
    directional = [row for row in evidence_rows if row['kind']=='chat'
        and str(row['user_id']) == str(fields['subject_id'])
        and _evidence_involves(row, fields['target_id'], db)]
    support = len(directional)
    days = sorted({_utc_day(row['at']) for row in directional if _utc_day(row['at'])})
    historical_max = max(prior_ids, default=0)
    if prior is not None:
        historical = db.execute("SELECT evidence_record_ids FROM social_assessments "
            "WHERE subject_id=? AND target_id=? AND provenance=? AND state IN ('active','superseded') "
            "AND " + scope + " ORDER BY id DESC LIMIT 512", params).fetchall()
        for item in historical:
            try:
                historical_max = max(historical_max, max((int(v) for v in json.loads(item[0] or '[]')),default=0))
            except (TypeError,ValueError):
                continue
    # A/B/A evidence cycling must not ratchet a score. Record IDs are monotonic.
    same_evidence = prior is not None and not any(v > historical_max for v in new_ids)
    confidence_cap = (55 if support == 2 else
                      85 if support >= 3 and len(days) >= 2 else
                      70 if support >= 3 else 0)
    high_support = support >= 3 and len(days) >= 2
    low, high = (20, 80) if high_support else (35, 65)
    prior_text = str(prior["impression"] or "") if prior is not None else ""
    fresh_text = " ".join(str(row["text"] or "") for row in evidence_rows)
    old_tokens = _tokens(prior_text)
    new_tokens = _tokens(fresh_text)
    if not prior_text.strip():
        novelty = 1.0
    elif not old_tokens or not new_tokens:
        novelty = 0.5
    else:
        overlap = len(old_tokens & new_tokens) / max(1, min(len(old_tokens), len(new_tokens)))
        novelty = _clamp(1.0 - overlap, 0.0, 1.0)
    proposed_conf = fields.get("confidence")
    if proposed_conf is None and prior is not None:
        proposed_conf = prior["confidence"]
    alpha = _clamp(0.15 + 0.35 * novelty * float(proposed_conf or 0) / 100.0, 0.15, 0.5)
    previous_scores = {}
    proposed_scores = {}
    applied_scores = {}
    deltas = {}
    for field in ("affinity", "closeness", "confidence"):
        proposed = fields.get(field)
        previous = prior[field] if prior is not None else None
        previous_scores[field] = previous
        proposed_scores[field] = proposed
        if field == "confidence":
            target = proposed if proposed is not None else (previous if prior is not None else None)
            cap = confidence_cap if confidence_cap else 0
            if same_evidence or support < 2:
                applied = previous
            elif target is None:
                applied = previous
            elif previous is None:
                applied = min(int(target), cap) if cap else None
            else:
                delta = _clamp(alpha * (float(target) - float(previous)), -12.0, 12.0)
                applied = int(round(float(previous) + delta))
                applied = int(_clamp(applied, 0, cap)) if cap else None
        else:
            if same_evidence or support < 2:
                applied = previous
            elif proposed is None:
                # No calendar decay and no automatic neutralisation: retain the
                # previous current projection when the new note has no score.
                applied = previous
            elif previous is None:
                applied = int(_clamp(int(proposed), low, high))
            else:
                delta = _clamp(alpha * (float(proposed) - float(previous)), -12.0, 12.0)
                applied = int(round(float(previous) + delta))
                applied = int(_clamp(applied, 0, 100))
        applied_scores[field] = applied
        deltas[field] = (None if previous is None or applied is None
                         else int(applied) - int(previous))
    if same_evidence:
        policy_reason = ("相同证据不重复漂移分数或抬高置信度；保留已有投影，原文照常保存。"
                         if prior is not None else
                         "没有可复用的同向Terra投影，本次只写描述。")
    elif support < 2:
        policy_reason = "直接互动证据不足2条，分数保持未知；可写描述性观察。"
    else:
        policy_reason = (f"按新证据加权更新（novelty={novelty:.2f}, alpha={alpha:.2f}, "
                         f"support={support}, days={len(days)}, confidence_cap={confidence_cap}），"
                         "单次变化最多12分；不等于对方内心或好感事实。")
    return {
        "previous": previous_scores["affinity"],
        "proposed": proposed_scores["affinity"],
        "applied": applied_scores["affinity"],
        "delta": deltas["affinity"],
        "impact": "terra_evidence",
        "policy_reason": policy_reason,
        "scores": {
            key: {"previous": previous_scores[key], "proposed": proposed_scores[key],
                  "applied": applied_scores[key], "delta": deltas[key]}
            for key in ("affinity", "closeness", "confidence")
        },
        "novelty": round(novelty, 3),
        "alpha": round(alpha, 3),
        "support": support,
        "days": days,
        "confidence_cap": confidence_cap,
        "same_evidence": same_evidence,
        "evidence_signature": sorted(new_ids),
    }


_UNSET = object()


def apply_manual_curve(db, fields, impact, now, *, previous=_UNSET,
                       previous_ids=None, previous_created_at=None,
                       previous_assessment_id=None):
    """Apply the manual affinity curve against a verified predecessor.

    Callers that know which historical assessment a blank field is inheriting
    pass its value/evidence explicitly.  The legacy four-argument call still
    resolves the newest same-session active row itself.  A blank proposal never
    invents a score: when a verified predecessor is supplied it is retained,
    otherwise affinity stays unknown.
    """
    from hakimi.social.social_base import _assessment_dict, _evidence_involves
    old_ids = list(previous_ids or [])
    if previous is _UNSET:
        public = str(fields['session']).startswith('group:')
        scope = "session LIKE 'group:%'" if public else "session=?"
        params = [fields['target_id']] + ([] if public else [fields['session']])
        prior = db.execute(
            "SELECT * FROM social_assessments WHERE subject_id='hakimi' AND target_id=? "
            "AND provider='gemini' AND provenance='gemini_manual' AND state='active' AND "
            + scope + " ORDER BY created_at DESC,id DESC LIMIT 1",
            params,
        ).fetchone()
        previous = None
        if prior is not None:
            try:
                old_ids = [int(value) for value in
                           json.loads(prior['evidence_record_ids'] or '[]')]
            except (ValueError, TypeError):
                old_ids = []
            evidence = []
            if old_ids:
                marks = ','.join('?' for _ in old_ids)
                evidence = db.execute(
                    "SELECT id,user_id,session,kind,at,state,text FROM records "
                    "WHERE id IN (" + marks + ") AND state!='deleted'", old_ids,
                ).fetchall()
            previous = _assessment_dict(prior, evidence_rows=evidence).get('affinity')
            previous_created_at = float(prior['created_at'] or 0)
            previous_assessment_id = int(prior['id'])
    proposed = fields['affinity']
    if proposed is None:
        result = {
            'previous': previous,
            'proposed': None,
            'applied': previous,
            'delta': 0,
            'impact': impact,
            'policy_reason': (
                '本次未给新分数，沿用已有可核对评分；没有重新打分，原文照常保存。'
                if previous is not None else
                '本次未评分，也没有可沿用的有证据旧评分；保持未知，原文照常保存。'
            ),
        }
        if previous_assessment_id is not None:
            result['source_assessment_id'] = int(previous_assessment_id)
        return result
    policy = adjust_affinity(previous, proposed, impact,
                             fields.get('confidence'), fields.get('reason', ''),
                             len(fields.get('evidence', [])))
    if previous is not None and proposed != previous:
        new_ids = {int(value) for value in fields.get('evidence', [])} - set(old_ids)
        fresh = []
        if new_ids:
            marks = ','.join('?' for _ in new_ids)
            rows = db.execute(
                'SELECT id,user_id,session,kind,at,state,text,direct_targets,reply_to '
                'FROM records WHERE id IN (' + marks + ')', sorted(new_ids),
            ).fetchall()
            fresh = [
                row for row in rows
                if row['state'] not in ('deleted', 'retracted')
                and str(row['text'] or '').strip()
                and float(previous_created_at or 0) < float(row['at']) <= now + 60
                and _evidence_involves(row, fields['target_id'], db)
            ]
        if not fresh:
            policy.update(
                applied=previous, delta=0,
                policy_reason='本次没有新的互动证据，好感不重复加减；文字印象仍可保存。',
            )
    if previous_assessment_id is not None:
        policy['source_assessment_id'] = int(previous_assessment_id)
    return policy
