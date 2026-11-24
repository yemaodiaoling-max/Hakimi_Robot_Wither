"""Deterministic lossy extraction: exact excerpts only; zero model calls."""
from .types import EXPORT_KINDS, SENSITIVE, MAX_FACTS, MAX_TEXT, references_valid, canonical


def compress(items, *, endpoint, now, max_items=8, char_budget=2400):
    if endpoint not in {'qq', 'bilibili'}:
        raise ValueError('invalid endpoint')
    if not 1 <= max_items <= MAX_FACTS or not 1 <= char_budget <= 8000:
        raise ValueError('invalid compression budget')
    newest = {}
    for item in items:
        key = (item.scope, item.source_id)
        if key not in newest or item.revision > newest[key].revision:
            newest[key] = item
    eligible = []
    for item in newest.values():
        if (item.imported or item.private or item.scope not in {'self', 'public'}
                or item.kind not in EXPORT_KINDS or SENSITIVE.search(item.text)
                or item.created_at > now
                or (item.kind in {'watch_note', 'episode'} and item.created_at < now-7*86400)):
            continue
        if not item.source_refs:
            continue
        references_valid(item.source_refs)
        if endpoint == 'bilibili' and (not item.coverage or not any(
            ref.startswith(('bilibili:video:', 'bilibili:live:')) for ref in item.source_refs)):
            continue
        eligible.append(item)
    eligible.sort(key=lambda x: (x.created_at, x.importance, x.revision, x.source_id), reverse=True)
    facts, seen_ids, seen_text = [], set(), set()
    for item in eligible:
        identity = (item.scope, item.source_id)
        normalized = ' '.join(item.text.split())
        if identity in seen_ids or normalized in seen_text:
            continue
        # Prefix comes directly from the source, never paraphrased or invented.
        excerpt = item.text[:min(360, MAX_TEXT)]
        if len(excerpt) > char_budget:
            continue  # The next packet must use the same immutable excerpt.
        if not excerpt.strip():
            continue
        fact = dict(source_id=item.source_id, revision=item.revision, scope=item.scope,
            text=excerpt, source_refs=list(item.source_refs), coverage=list(item.coverage),
            attribution=item.attribution, created_at=item.created_at,
            importance=item.importance, truncated=item.pre_truncated or len(excerpt) < len(item.text))
        if len(canonical([*facts, fact]).encode('utf-8')) > 24_000:
            continue
        facts.append(fact)
        seen_ids.add(identity)
        seen_text.add(normalized)
        char_budget -= len(excerpt)
        if len(facts) >= max_items or char_budget <= 0:
            break
    return facts


def compress_batches(items, *, endpoint, now):
    """Bound each packet without checkpointing past unsent eligible source items."""
    remaining, batches, texts = list(items), [], set()
    while remaining:
        facts = compress(remaining, endpoint=endpoint, now=now)
        if not facts:
            break
        fresh = [fact for fact in facts if ' '.join(fact['text'].split()) not in texts]
        identities = {(fact['scope'], fact['source_id']) for fact in facts}
        texts.update(' '.join(fact['text'].split()) for fact in facts)
        remaining = [item for item in remaining if (item.scope, item.source_id) not in identities]
        if fresh:
            batches.append(fresh)
    return batches
