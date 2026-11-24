"""Directional relationship evidence, independent of person-information notes."""
import json
import re


def reaction_sources(db, row, context=()):
    """Actual SELF speech and event-linked self-written person notes only."""
    origin = str(row['origin'])
    at = float(row['at'])
    self_id = str(row['self_id'] or '')
    replies, notes = [], []
    def add_reply(item):
        if item.get('kind') == 'dialogue':return
        if not (item.get('speaker') == 'SELF' or (str(item.get('user_id', '')) == 'SELF' or bool(self_id and str(item.get('user_id', '')) == self_id))):return
        when = float(item.get('at') or 0)
        refs = item.get('reply_to') or []
        refs = [str(refs)] if isinstance(refs, (str, int)) else [str(value) for value in refs]
        if refs and origin not in refs:return
        if when < at or (origin not in refs and when-at > 180):return
        text = str(item.get('text') or '')
        if text.strip() and text not in replies:replies.append(text)
    for item in context:add_reply(dict(item))
    # These windows contain acknowledged outgoing bubbles and exact target IDs.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reply_review_windows'").fetchone():
        windows = db.execute("SELECT human_ids,replies FROM reply_review_windows WHERE session=? AND updated_at>=? AND created_at<=? AND replies!='[]' ORDER BY updated_at DESC LIMIT 8",
                             (row['session'], at, at+900)).fetchall()
        for window in windows:
            try:
                if origin not in {str(value) for value in json.loads(window['human_ids'])}:continue
                for item in json.loads(window['replies']):add_reply(item)
            except (ValueError, TypeError, AttributeError):continue
    if self_id:
        for item in db.execute("SELECT user_id,kind,text,at,reply_to FROM records WHERE session=? AND kind='chat' AND user_id=? AND state='active' AND at BETWEEN ? AND ? ORDER BY at LIMIT 4",
                               (row['session'], self_id, at, at+180)):
            add_reply(dict(item))
    originals = db.execute("SELECT o.text,o.metadata FROM memory_originals o JOIN records r ON r.id=o.record_id "
                           "WHERE o.session=? AND o.user_id=? AND o.kind='person' AND o.provider IN ('gemini','dsh') "
                           "AND r.state NOT IN ('deleted','retracted') AND o.created_at>=? ORDER BY o.created_at DESC,o.id DESC LIMIT 8",
                           (row['session'], row['user_id'], at)).fetchall()
    for note in originals:
        try:meta=json.loads(note['metadata'] or '{}')
        except (ValueError,TypeError):continue
        if not isinstance(meta, dict):continue
        linked = str(meta.get('origin_message_id') or '') == origin
        linked = linked or ('M'+str(row['id'])) in {str(value) for value in meta.get('related_memory_ids', [])}
        if linked and note['text'] and note['text'] not in notes:notes.append(note['text'])
    return {'replies': replies[:3], 'notes': notes[:2]}


def packet(db, row):
    try:
        mentions = json.loads(row['direct_targets'] or '[]')
    except (ValueError, TypeError):
        mentions = []
    if not isinstance(mentions, list):mentions = []
    if not row['direct_targets']:mentions = re.findall(r'\[CQ:at,qq=(\d+)\]', row['text'])
    reply = db.execute("SELECT user_id,nickname FROM records WHERE kind='chat' AND session=? AND origin=? LIMIT 1",
                       (row['session'], row['reply_to'])).fetchone() if row['reply_to'] else None
    context = []
    if not mentions and not reply and row['self_id']:
        nearby = db.execute("SELECT id,kind,key,user_id,self_id,at,text,direct_targets,reply_to FROM records WHERE session=? "
                            "AND state='active' AND text!='' AND ((kind='chat' AND user_id=?) OR kind='dialogue') "
                            "AND at BETWEEN ? AND ? ORDER BY ABS(at-?),id LIMIT 2",
                            (row['session'], row['self_id'], row['at'] - 60, row['at'] + 60, row['at'])).fetchall()
        for r in nearby:
            value=dict(r)
            if r['kind']=='dialogue':
                # Embedded newlines in the question make the speaker boundary
                # ambiguous. Keep a labeled combined excerpt, never invent one.
                value['label']='Combined question and actually sent Hakimi reply; internal speaker boundary is unresolved'
                value['text']=r['text'][-140:]
            else:value['text']=r['text'][:140]
            context.append(value)
    reactions = reaction_sources(db, row, context)
    return {'reaction_replies': [text[:240] for text in reactions['replies']], 'reaction_notes': [text[-360:] for text in reactions['notes']],
            'id': row['id'], 'author': row['user_id'], 'author_name': str(row['nickname'] or ''), 'self_id': row['self_id'],
            'session': row['session'], 'mentions': mentions, 'at': row['at'],
            'reply_author': reply[0] if reply else '', 'reply_author_name': str(reply['nickname'] or '') if reply else '', 'text': row['text'],
            'context': [dict(r) for r in context]}


def directed(p):
    """Explicit mentions override replies; implicit turns require nearby bot speech."""
    author = str(p['author']);self_id = str(p.get('self_id') or '')
    if not author.isdigit() or author in {self_id, 'SELF'}:return False
    refs = {str(v) for v in p.get('mentions', [])}
    if refs:return bool(self_id and refs == {self_id})
    reply = str(p.get('reply_author') or '')
    if reply:return reply in {self_id, 'SELF'}
    if str(p.get('session', '')).startswith('private:'):return True
    return any((r.get('kind')=='dialogue' or str(r['user_id'])==self_id) and r['text'].strip()
               for r in p.get('context', []) if self_id)


def candidate(db, row):
    # Low-information social turns share the ordinary daily queue and its throttle.
    text = re.sub(r'\[CQ:[^\]]*\]', '', row['text']).strip()
    return bool(text and directed(packet(db, row)))


def duplicate(db, scope, target, row, item):
    """Exact sources are permanent; a short conversation episode is bounded in time.

    Both signs use the same rule. A later fresh conflict, even identical wording,
    remains eligible rather than being suppressed by an everlasting text hash.
    """
    old = db.execute('SELECT e.event_id,e.affinity_delta,e.closeness_delta FROM impression_effects e LEFT JOIN records r ON r.id=e.event_id '
                     'WHERE e.scope=? AND e.target=? AND (e.event_id=? OR '
                     '(r.session=? AND r.at BETWEEN ? AND ? AND (e.affinity_delta!=0 OR e.closeness_delta!=0)))',
                     (scope, target, row['id'], row['session'], row['at']-120, row['at']+120)).fetchall()
    for item_effect in old:
        if item_effect[0] == row['id']:return True
        prior = db.execute('SELECT * FROM records WHERE id=?', (item_effect[0],)).fetchone()
        if not prior or prior['session'] != row['session']:continue
        affinity={'positive':1,'negative':-1,'neutral':0}[item['valence']]
        closeness={'closer':1,'further':-1,'unchanged':0}[item['closeness']]
        sign=lambda v:(v>0)-(v<0)
        if abs(prior['at'] - row['at']) <= 120 and sign(item_effect['affinity_delta'])==affinity and sign(item_effect['closeness_delta'])==closeness:
            if item.get("valence") == "negative":
                escalated_types = {"pestering_or_spam", "targeted_insult", "boundary_violation", "threat", "privacy_harm"}
                if item.get("event_type") in escalated_types:
                    continue
                new_neg_change = item.get("host_changes", (0, 0))[0]
                if new_neg_change < item_effect["affinity_delta"]:
                    continue
            return True
    return False


def validate(items, packets):
    if not isinstance(items, list) or len(items) > 2:raise ValueError('invalid_relationship_events')
    allowed = {p['id']: p for p in packets};result = [];seen = set()
    for item in items:
        if not isinstance(item, dict):raise ValueError('invalid_relationship_event')
        event = item.get('event_id');p = allowed.get(event)
        if p is None or event in seen:raise ValueError('invalid_relationship_source')
        seen.add(event)
        if item.get('kind')!='relationship' or item.get('directed_to_hakimi') is not True or not directed(p):continue
        from hakimi.memory.relationship_policy import normalize_event
        result.append(normalize_event(
            dict(item, id=event), p['text'],
            reply_texts=p.get('reaction_replies', ()),
            note_texts=p.get('reaction_notes', ()),
        ))
    return result
