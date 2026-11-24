"""Small deterministic consolidation rules; no network or model calls."""
import hashlib
import json
from pathlib import Path
import re
import time
from hakimi.memory.lifecycle_rules import SYNONYMS, discussion_episode, expanded_terms, explicit_person, source_url, sourced_reply  # noqa: F401 (re-export)

REVISION = '2026-10-27-memory-v3'
SOCIAL_OPPORTUNITY_REVISION = '2026-11-15-memory-v5'
SOCIAL_COOLDOWN_SECONDS = 6 * 3600
SOCIAL_SAME_EVIDENCE_SECONDS = 24 * 3600
MAX_SOCIAL_EVIDENCE = 4
SOCIAL_HINT_COOLDOWN_SECONDS = 6 * 3600
SOCIAL_TRIGGERS = (
    (r'记住|记一下|别忘了|以后叫我|称呼我|改口', '明确的称呼、约定或长期记忆请求'),
    (r'更正|纠正|说错了|不是.{0,12}是|改成|更新一下', '可能的重要更正'),
    (r'约定|答应|说好|下周|以后一起|提醒我|承诺', '可能的重要约定'),
    (r'一起完成|做完了|搞定了|整理好|写完|提交了|完成了|解决了', '共同完成的任务'),
    (r'抱歉|对不起|刚才是我|别生气|误会|和解|不是针对', '可能的冲突修复'),
    (r'(?:讨论|聊|问).{0,24}(?:清楚|明白|有用|帮到|解决|一致)', '有帮助的讨论收束'),
)
PROVENANCE_LABELS = {
    'gemini_manual': 'Gemini 亲笔',
    'dsh_assisted': 'DSH 辅助整理',
    'terra_assisted': 'Terra 观察（辅助，非当事人内心）',
}


def impression_excerpt(text, limit=280):
    """Keep stable profile context and the latest appended observations in view."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    head = max(0, (limit - 1) // 2)
    return text[:head] + "…" + text[-(limit - head - 1):]


def social_signal(text):
    """Return a conservative reason for a possible *social* impression.

    Ordinary @s, greetings, jokes, conflict bait and every-turn chat are not
    signals.  The caller still has to validate current evidence and a cooldown.
    """
    text = str(text or '').strip()
    if len(text) < 6 or len(text) > 800:
        return None
    if any(value in text for value in ('[合并转发', '[转发', '[CQ:forward', '[CQ:json', '[CQ:xml')):
        return None
    if re.search(r'密码|身份证|手机号|病史|诊断|性癖|自杀|自残', text):
        return None
    for pattern, reason in SOCIAL_TRIGGERS:
        if re.search(pattern, text):
            return reason
    return None


def _social_state_path(base):
    return Path(base) / 'state' / 'social_opportunity.json'


def _read_social_state(base):
    try:
        value = json.loads(_social_state_path(base).read_text(encoding='utf-8'))
        if isinstance(value, dict) and isinstance(value.get('entries'), dict):
            return value
    except (OSError, ValueError):
        pass
    return {'version': 1, 'entries': {}}


def _write_social_state(base, value):
    path = _social_state_path(base)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        temp.replace(path)
    except OSError:
        pass


def _origin_id(value):
    """Official QQ message ids may be numeric or opaque; only bound them."""
    if isinstance(value, bool) or value is None:
        return ''
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if not value or len(value) > 160:
        return ''
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return ''
    return value


def _evidence_rows(base, session, messages, target, now):
    """Current chat M ids, restricted to this exact session and target."""
    target_id = str(target.get('user_id', '') or '')
    if not re.fullmatch(r'[1-9][0-9]{0,19}', target_id):
        return []
    origins = []
    for row in list(messages or [])[-24:] + [target]:
        value = _origin_id(row.get('message_id', ''))
        if value:
            origins.append(value)
    origins = list(dict.fromkeys(origins))
    if not origins:
        return []
    from hakimi.memory.memory_store import store
    index = store(str(Path(base).resolve()))
    marks = ','.join('?' for _ in origins)
    try:
        with index.db() as db:
            rows = db.execute(
                "SELECT id,user_id,session,at,text,direct_targets FROM records "
                "WHERE kind='chat' AND state!='deleted' AND session=? AND origin IN (" + marks + ") "
                "ORDER BY at DESC,id DESC LIMIT 24", [session, *origins],
            ).fetchall()
    except Exception:
        return []
    evidence = []
    for row in rows:
        author = str(row['user_id'] or '')
        if author != target_id:
            continue
        if not str(row['text'] or '').strip() or float(row['at'] or 0) > now + 60:
            continue
        evidence.append({
            'memory_id': 'M' + str(row['id']), 'id': int(row['id']),
            'at': float(row['at'] or 0), 'text': str(row['text'] or '')[:240],
        })
    return evidence[:MAX_SOCIAL_EVIDENCE]


def _self_ids(base):
    result = set()
    try:
        cfg = json.loads((Path(base) / 'config.json').read_text(encoding='utf-8'))
        value = str((cfg.get('bot_profile') or {}).get('self_qq', ''))
        if re.fullmatch(r'[1-9][0-9]{0,19}', value):
            result.add(value)
    except (OSError, ValueError, AttributeError):
        pass
    return result


def _row_targets(row):
    value = row.get('direct_targets', '') if hasattr(row, 'get') else row['direct_targets']
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, (list, tuple)):
        return {str(item) for item in parsed if re.fullmatch(r'[1-9][0-9]{0,19}', str(item))}
    return {item for item in re.findall(r'[1-9][0-9]{0,19}', str(value or ''))}


def _token_set(text):
    try:
        from hakimi.memory.text import terms
        return set(terms(str(text or '')))
    except Exception:
        value = re.sub(r'\s+', '', str(text or ''))
        return {value[index:index + 2] for index in range(max(0, len(value) - 1))}


def impression_novelty(previous, excerpts):
    """Bounded 0..1 heuristic: lower overlap with the previous own note = newer."""
    previous = str(previous or '')
    if not previous.strip():
        return 1.0
    old = _token_set(previous)
    fresh = _token_set(' '.join(str(value) for value in excerpts if value))
    if not old or not fresh:
        return 0.5
    overlap = len(old & fresh) / max(1, min(len(old), len(fresh)))
    return max(0.0, min(1.0, 1.0 - overlap))


def _interaction_summary(base, session, target_id, anchor, now, self_ids):
    """Bounded read-only count of this person's new messages and direct events."""
    since = max(float(anchor or 0), now - 30 * 86400)
    from hakimi.memory.memory_store import store
    index = store(str(Path(base).resolve()))
    with index.db() as db:
        rows = db.execute(
            "SELECT id,origin,user_id,session,at,text,direct_targets,reply_to,self_id "
            "FROM records WHERE kind='chat' AND state!='deleted' AND session=? AND at>=? "
            "ORDER BY at DESC,id DESC LIMIT 400", (session, since),
        ).fetchall()
    origins = {}
    self_set = set(self_ids)
    for row in rows:
        value = str(row['self_id'] or '')
        if re.fullmatch(r'[1-9][0-9]{0,19}', value):
            self_set.add(value)
        if row['origin']:
            origins[(str(row['session']), str(row['origin']))] = str(row['user_id'] or '')
    direct = 0
    total = 0
    days = set()
    signals = []
    latest_signal = ''
    latest_signal_at = 0.0
    for row in rows:
        author = str(row['user_id'] or '')
        if author != str(target_id):
            continue
        total += 1
        day = time.strftime('%Y-%m-%d', time.gmtime(float(row['at'] or 0)))
        days.add(day)
        refs = _row_targets(row)
        reply = str(row['reply_to'] or '')
        if reply:
            author_of_reply = origins.get((str(row['session']), reply))
            if author_of_reply in self_set:
                refs.add('__self__')
        if '__self__' in refs or refs & self_set:
            direct += 1
        signal = social_signal(str(row['text'] or ''))
        if signal:
            signals.append((float(row['at'] or 0), signal))
            if float(row['at'] or 0) >= latest_signal_at:
                latest_signal_at = float(row['at'] or 0)
                latest_signal = signal
    return {
        'direct_count': direct,
        'message_count': total,
        'days': sorted(day for day in days if day),
        'latest_signal': latest_signal,
        'signals': signals[-4:],
    }


def _latest_social(session, target_id, base):
    """Public groups share prior own notes; private notes stay in their session."""
    from hakimi.memory.memory_store import store
    index = store(str(Path(base).resolve()))
    scope = "session LIKE 'group:%'" if str(session).startswith('group:') else "session=?"
    args = [target_id] + ([] if str(session).startswith('group:') else [session])
    try:
        with index.db() as db:
            rows = db.execute("SELECT id,impression,created_at,provenance,affinity,closeness,confidence FROM social_assessments "
                "WHERE target_id=? AND subject_id='hakimi' AND state='active' AND " + scope +
                " ORDER BY created_at DESC,id DESC", args).fetchall()
            if rows:
                latest = rows[0]
                own = next((r for r in rows if r['provenance']=='gemini_manual'),None)
                scored = own or next((r for r in rows if r['provenance']=='dsh_assisted'),None)
                provenance = str(latest['provenance'] or '')
                return {'last_write_at':float(latest['created_at'] or 0),
                    'last_impression':impression_excerpt(latest['impression']),
                    'provenance':provenance,'source':PROVENANCE_LABELS.get(provenance,provenance),
                    'gemini_write_at':float(own['created_at'] or 0) if own else 0,
                    'gemini_impression':impression_excerpt(own['impression']) if own else '',
                    'affinity':scored['affinity'] if scored else None,
                    'closeness':scored['closeness'] if scored else None,
                    'confidence':scored['confidence'] if scored else None,
                    'assessment_id':int(scored['id']) if scored else None}
    except (OSError, sqlite3.Error):
        return None
    return None


def social_opportunity(session, messages, target, base, now, reason=None):
    """Offer an optional notebook cue for new visible interaction.

    Cooldown and evidence dedup limit reminders, never manual writing. Merely
    seeing a reminder does not spend a finite chance to write later.
    """
    if not str(session).startswith('group:') and not str(session).startswith('private:'):
        return None
    target_id = str(target.get('user_id', '') or '')
    if not re.fullmatch(r'[1-9][0-9]{0,19}', target_id):
        return None
    text = str(target.get('text', '') or '')[:800]
    evidence = _evidence_rows(base, session, messages, target, now)
    if not evidence:
        return None
    latest = _latest_social(session, target_id, base) or {}
    evidence = [row for row in evidence if row['at'] > float(latest.get('gemini_write_at', 0) or 0)]
    if not evidence:
        return None
    evidence_ids = [row['memory_id'] for row in evidence]
    own_write_at = float(latest.get('gemini_write_at', 0) or 0)
    own_impression = str(latest.get('gemini_impression', '') or '')
    self_ids = _self_ids(base)
    try:
        summary = _interaction_summary(base, session, target_id, own_write_at, now, self_ids)
    except (OSError, sqlite3.Error, ValueError):
        summary = {'direct_count': 0, 'message_count': 0, 'days': [],
                   'latest_signal': '', 'signals': []}
    novelty = impression_novelty(own_impression, [row['text'] for row in evidence])
    reason = (reason or social_signal(text) or '刚和这个人聊过，可以随手记下自己的印象')[:120]

    state = _read_social_state(base)
    hints = state.setdefault('hints', {})
    if not isinstance(hints, dict):
        hints = {}
        state['hints'] = hints
    hint_key = ('public:' + target_id if str(session).startswith('group:') else str(session) + '|' + target_id)
    hint = hints.get(hint_key) if isinstance(hints.get(hint_key), dict) else {}
    evidence_max = max(int(row['id']) for row in evidence)
    if evidence_max <= int(hint.get('max_evidence_id', 0) or 0):
        return None
    day = time.strftime('%Y-%m-%d', time.gmtime(float(now)))
    daily_count = int(hint.get('count', 0) or 0) if hint.get('day') == day else 0
    if hint and now - float(hint.get('at', 0) or 0) < SOCIAL_HINT_COOLDOWN_SECONDS:
        return None
    digest = hashlib.sha256(
        (target_id + '|' + str(session) + '|' + ','.join(evidence_ids)
         + '|' + str(int(summary.get('direct_count', 0)))).encode()
    ).hexdigest()[:32]
    if hint.get('digest') == digest:
        # Identical evidence is never re-offered, even after the cooldown.
        return None

    entries = state.setdefault('entries', {})
    previous = entries.get(session + '|' + target_id)
    if isinstance(previous, dict):
        last = float(previous.get('at', 0) or 0)
        if now - last < SOCIAL_COOLDOWN_SECONDS:
            return None
        if previous.get('digest') == digest:
            return None
    from hakimi.social.social_behavior import behavior_for
    own = behavior_for(base, target_id, session, now=now)
    previous_scores = {
        'affinity': own['affinity'] if own['affinity'] is not None else (latest or {}).get('affinity'),
        'closeness': (latest or {}).get('closeness'),
        'confidence': own['confidence'] if own['confidence'] is not None else (latest or {}).get('confidence'),
        'assessment_id': own['assessment_id'] or (latest or {}).get('assessment_id'),
        'known': own['known'],
    }
    entries[session + '|' + target_id] = {'at': float(now), 'digest': digest, 'reason': reason}
    # Bounded audit state; newest 200 conversation entries and 200 people.
    if len(entries) > 200:
        for key in sorted(entries, key=lambda value: float((entries[value] or {}).get('at', 0)))[:-200]:
            entries.pop(key, None)
    hints[hint_key] = {'at': float(now), 'day': day, 'count': daily_count + 1,
                        'max_evidence_id': evidence_max,
                        'digest': digest, 'reason': reason}
    if len(hints) > 200:
        for key in sorted(hints, key=lambda value: float((hints[value] or {}).get('at', 0)))[:-200]:
            hints.pop(key, None)
    _write_social_state(base, state)
    return {
        'reason': reason,
        'target_id': target_id,
        'previous_scores': previous_scores,
        'evidence_memory_ids': evidence_ids,
        'evidence': [{'memory_id': row['memory_id'], 'date': time.strftime(
            '%Y-%m-%d', time.gmtime(row['at'])), 'excerpt': row['text']} for row in evidence],
        'last_impression': own_impression or (latest or {}).get('last_impression', ''),
        'last_write_at': (latest or {}).get('last_write_at', 0),
        'last_write_date': (time.strftime('%Y-%m-%d', time.gmtime(latest['last_write_at']))
                            if latest and latest.get('last_write_at') else ''),
        'last_impression_source': (latest or {}).get('source', ''),
        'last_write_provenance': (latest or {}).get('provenance', ''),
        'last_gemini_write_at': own_write_at,
        'new_since_write': int(summary.get('direct_count', 0)),
        'direct_new_count': int(summary.get('direct_count', 0)),
        'novelty': round(float(novelty), 3),
        'cooldown_seconds': SOCIAL_HINT_COOLDOWN_SECONDS,
        'guidance': ('人物小本子是你自己的随手记：随手在原印象后添一句新观察、吐槽或小细节用memory.social_append；'
                     '初次建档或印象变化较大时完整重写用memory.social。长短措辞自己定，note原文逐字保存；'
                     '混熟了或有新体感时可顺手给/微调closeness（亲近）、affinity（好感）和confidence，分数可省略自动沿用可用旧值。'
                     '小本子遵循核心长留、琐事随忘：稳定人设、背景定位与约定长久保留，一时兴起的日常琐事会自动随时间精简淡忘。提醒只是可选提示，不写不会耗尽以后写的机会。'),
    }

def affinity_guidance():
    return ('好感与亲近评分：正常分歧、批评、拒绝、双方接受的玩笑不算严重负面；持续恶意骚扰、威胁或隐私侵害须有真实证据才可作重大负面；'
            '实际帮助、重要履约或长期可靠表现作重大正面（可用impact=major、confidence≥75和reason）。'
            '普通好感变化每次最多3分（低于20普通负面不再扣分，高于80普通正面不再加分）。'
            '跟对方混熟了、聊得投机或有新体感时，可顺手给/微调closeness（亲近度0-100）、affinity（好感0-100，默认基准50）和confidence（把握0-100，≥60生效）；'
            '不每轮打分，分数可留空自动沿用同人物同作用域内可用的旧分数，以工具回执为准。')


def guidance():
    return (affinity_guidance() + '\n'
            '长期记忆与人物小本子：只有收到成功回执才算记入，记录的是你自己的主观感受与判断，不是对方内心。'
            '想留下的观察、吐槽、喜欢或反感、当时的小感受，可用memory.remember（kind可为person/preference/lore/episode/reflection/commitment，自己的理解可用reflection）。'
            '人物小本子分两种调用：随手在原印象后添一句新细节或吐槽用memory.social_append（自动拼接保留原画像）；'
            '初次建档或印象发生较大改观时用memory.social写立体鲜活的第一人称人物画像（性格脾气、常聊话题、相处体感与具体细节）。'
            '长短措辞自己定，用当前人设的自然口吻，note原文逐字保存，不改写、不截断。'
            '小本子遵循核心长留、琐事随忘：稳定人设、背景定位与约定长久保留，一时兴起的日常琐事会自动随时间精简淡忘。'
            '想起别群聊过的事，可用memory.search的all_groups接着找。\n')
