"""Bounded, local weighted diary selection; no model calls or database writes."""
from datetime import datetime, time as walltime, timedelta
import json
import logging
import math
import random
import re
import sqlite3
import time

from hakimi.core import owner
from hakimi.features.diary_store import ZONE, entries
from hakimi.memory.text import terms

TODAY_BUCKET_LIMIT = 32
PAST_DAY_LIMIT = 16
MAX_GROUPS = 3
TEXT_BUDGET = 3600
GROUP_BUDGET = 1200
EXCERPT_LIMIT = 280
FRESHNESS_WEIGHT = 1.8
INVOLVEMENT_WEIGHT = 1.4
EXPLICIT_REPLY_WEIGHT = 0.8
MULTI_SPEAKER_WEIGHT = 0.3
QUESTION_WEIGHT = 0.6
INFORMATION_WEIGHT = 0.6

FIELDS = 'id,session,user_id,text,at,origin,self_id,direct_targets,reply_to'
FIELDS_WITH_NICKNAME = FIELDS + ',nickname'
VALID = "kind='chat' AND state='active' AND session LIKE 'group:%' AND origin!='' AND text!='' AND (expires=0 OR expires>?)"
CQ_AT_RE = re.compile(r'\[CQ:at,([^\]]+)\]')
PERSON_ALIAS_KEYS = ('称呼', '外号', '昵称', 'alias', 'nick')


def normalize(text):
    return re.sub(r'\s+|[^\w\u4e00-\u9fff]', '', text.lower())


def seed_for_day(day):
    return 'diary:'+day


def _clean_display_name(value, user_id=''):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()[:24].strip()
    uid = str(user_id or '').strip()
    if not text or re.fullmatch(r'\d+', text) or re.fullmatch(r'用户\d+', text) or (uid and text == uid):
        return ''
    return text


def _parse_cq_at_params(raw_params):
    uid = ''
    cq_name = ''
    for part in raw_params.split(','):
        if '=' not in part:
            continue
        key, val = part.split('=', 1)
        key = key.strip().lower()
        val = val.strip()
        if key == 'qq' and not uid:
            uid = val
        elif key == 'name' and not cq_name:
            cq_name = val
    return uid, cq_name


def _extract_cq_at_uids(text):
    uids = []
    for match in CQ_AT_RE.finditer(str(text or '')):
        uid, _ = _parse_cq_at_params(match.group(1))
        if uid and uid != 'all' and uid != 'SELF':
            uids.append(uid)
    return uids


def _clean_cq_mentions(text, row_self_id, bot_id, resolved_primary):
    def replace(match):
        uid, cq_name = _parse_cq_at_params(match.group(1))
        if not uid:
            return '@群友'
        if uid == 'all':
            return '@全体成员'
        if (row_self_id and uid == row_self_id) or (bot_id and bot_id != '0' and uid == bot_id):
            return '@自己'
        if uid in resolved_primary:
            return '@' + resolved_primary[uid]
        cleaned_cq = _clean_display_name(cq_name, uid)
        if cleaned_cq:
            return '@' + cleaned_cq
        return '@群友'

    return CQ_AT_RE.sub(replace, str(text or ''))


def _dedupe_names(items, exclude=''):
    seen = {exclude} if exclude else set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _resolve_uid_candidates(db, uid, session, now, row_nicknames, has_nickname, has_key, has_graph_aliases, owner_ids, owner_call):
    """Resolve primary display name candidate and public-group aliases for one user_id."""
    aliases = []
    group_nick = _clean_display_name(row_nicknames.get(uid, ''), uid)

    if not group_nick and has_nickname:
        same_group_rows = db.execute(
            "SELECT nickname FROM records WHERE kind='chat' AND state='active' "
            "AND session=? AND user_id=? AND nickname IS NOT NULL AND TRIM(nickname)!='' "
            "ORDER BY at DESC, id DESC LIMIT 8",
            (session, uid),
        ).fetchall()
        for r in same_group_rows:
            cleaned = _clean_display_name(r['nickname'], uid)
            if cleaned:
                group_nick = cleaned
                break

    if not group_nick and has_nickname:
        any_group_rows = db.execute(
            "SELECT nickname FROM records WHERE kind='chat' AND state='active' "
            "AND session LIKE 'group:%' AND user_id=? AND nickname IS NOT NULL AND TRIM(nickname)!='' "
            "ORDER BY at DESC, id DESC LIMIT 8",
            (uid,),
        ).fetchall()
        for r in any_group_rows:
            cleaned = _clean_display_name(r['nickname'], uid)
            if cleaned:
                group_nick = cleaned
                break

    if has_graph_aliases:
        alias_rows = db.execute(
            "SELECT alias FROM graph_aliases WHERE node_id=? AND session LIKE 'group:%' "
            "ORDER BY (session=?) DESC, last_seen DESC LIMIT 12",
            ('person:' + uid, session),
        ).fetchall()
        graph_names = []
        for r in alias_rows:
            cleaned = _clean_display_name(r['alias'], uid)
            if cleaned and cleaned not in graph_names:
                graph_names.append(cleaned)
                if len(graph_names) >= 3:
                    break
        aliases.extend(graph_names)

    person_rows = (
        db.execute(
            "SELECT key, text FROM records WHERE kind='person' AND state='active' "
            "AND session LIKE 'group:%' AND user_id=? AND (expires=0 OR expires>?) "
            "ORDER BY (session=?) DESC, at DESC, id DESC LIMIT 20",
            (uid, now, session),
        ).fetchall()
        if has_key
        else []
    )
    for r in person_rows:
        key_lower = str(r['key'] or '').lower()
        if not any(token in key_lower for token in PERSON_ALIAS_KEYS):
            continue
        raw_text = re.sub(r'\s+', ' ', str(r['text'] or '')).strip()
        if len(raw_text) > 32:
            continue
        cleaned = _clean_display_name(raw_text, uid)
        if cleaned:
            aliases.append(cleaned)

    if uid in owner_ids:
        primary = owner_call or '主人'
        all_aliases = _dedupe_names(([group_nick] if group_nick else []) + aliases, exclude=primary)
        return primary, all_aliases

    primary = group_nick
    if primary:
        all_aliases = _dedupe_names(aliases, exclude=primary)
        return primary, all_aliases

    deduped = _dedupe_names(aliases)
    if deduped:
        return deduped[0], deduped[1:]
    return '', []


def _resolve_group_names(db, ordered_rows, session, now, has_nickname, has_key, has_graph_aliases):
    """Resolve consistent speaker names, @-mention names, and speaker_aliases for one group."""
    owner_ids = owner.owner_ids()
    owner_call = _clean_display_name(owner.short_name()) or _clean_display_name(owner.owner_name())

    speaker_uids = []
    mention_uids = []
    row_nicknames = {}

    for row in ordered_rows:
        uid = str(row['user_id'] or '')
        if uid and uid != 'SELF':
            if uid not in speaker_uids:
                speaker_uids.append(uid)
            if has_nickname and 'nickname' in row.keys():
                cleaned = _clean_display_name(row['nickname'], uid)
                if cleaned:
                    row_nicknames[uid] = cleaned
        for at_uid in _extract_cq_at_uids(row['text']):
            if at_uid not in mention_uids:
                mention_uids.append(at_uid)

    all_uids = speaker_uids + [u for u in mention_uids if u not in speaker_uids]
    candidates = {}
    for uid in all_uids:
        candidates[uid] = _resolve_uid_candidates(
            db, uid, session, now, row_nicknames, has_nickname, has_key, has_graph_aliases, owner_ids, owner_call
        )

    # Disambiguate collisions among resolved primaries using distinct aliases.
    primary_map = {}
    aliases_map = {}
    used_primaries = set()
    anon_count = 0

    for uid in speaker_uids:
        primary, aliases = candidates[uid]
        if not primary:
            anon_count += 1
            primary = '群友' + str(anon_count)
        elif primary in used_primaries:
            replacement = next((a for a in aliases if a not in used_primaries), '')
            if replacement:
                aliases = _dedupe_names([primary] + aliases, exclude=replacement)
                primary = replacement
        used_primaries.add(primary)
        primary_map[uid] = primary
        aliases_map[uid] = _dedupe_names(aliases, exclude=primary)

    for uid in mention_uids:
        if uid in primary_map:
            continue
        primary, aliases = candidates[uid]
        if primary:
            if primary in used_primaries:
                replacement = next((a for a in aliases if a not in used_primaries), '')
                if replacement:
                    primary = replacement
            used_primaries.add(primary)
            primary_map[uid] = primary

    speaker_aliases = {}
    for uid in speaker_uids:
        speaker_name = primary_map[uid]
        extra = aliases_map.get(uid, [])[:2]
        if extra:
            speaker_aliases[speaker_name] = extra

    return primary_map, speaker_aliases


def candidate_rows(db, day, now, fields=FIELDS):
    """Latest records from calendar buckets, not a full-history random sample."""
    date = datetime.fromisoformat(day).date()
    buckets = [(datetime.combine(date, walltime(hour), ZONE),
                datetime.combine(date, walltime(hour+6), ZONE) if hour<18 else
                datetime.combine(date+timedelta(days=1), walltime(), ZONE), TODAY_BUCKET_LIMIT)
               for hour in (0,6,12,18)]
    for age in range(1,7):
        previous=date-timedelta(days=age)
        buckets.append((datetime.combine(previous,walltime(),ZONE),
                        datetime.combine(previous+timedelta(days=1),walltime(),ZONE),PAST_DAY_LIMIT))
    result=[]
    for start,end,limit in buckets:
        until=min(end.timestamp(),now)
        if start.timestamp()>=until:
            continue
        result.extend(db.execute('SELECT '+fields+' FROM records WHERE '+VALID+
            ' AND at>=? AND at<? ORDER BY at DESC,id DESC LIMIT ?',
            (now,start.timestamp(),until,limit)).fetchall())
    return result


def involved(row):
    try: targets=json.loads(row['direct_targets'] or '[]')
    except (ValueError,TypeError): targets=[]
    return row['user_id']=='SELF' or bool(row['self_id'] and isinstance(targets,list)
        and str(row['self_id']) in [str(t) for t in targets])


def context_rows(db, anchor, now, fields=FIELDS):
    """Independently fetch actual neighbours, rather than joining sampled buckets."""
    params=(now,anchor['session'])
    before=db.execute('SELECT '+fields+' FROM records WHERE '+VALID+
        ' AND session=? AND at>=? AND (at<? OR (at=? AND id<?)) ORDER BY at DESC,id DESC LIMIT 2',
        (*params,anchor['at']-300,anchor['at'],anchor['at'],anchor['id'])).fetchall()
    after=db.execute('SELECT '+fields+' FROM records WHERE '+VALID+
        ' AND session=? AND at<=? AND at<? AND (at>? OR (at=? AND id>?)) ORDER BY at,id LIMIT 2',
        (*params,anchor['at']+300,now,anchor['at'],anchor['at'],anchor['id'])).fetchall()
    return sorted([*before,anchor,*after],key=lambda r:(r['at'],r['id']))


def score(row, neighbours, reference, recent_terms):
    age=max(0,(reference-row['at'])/86400)
    explicit=bool(row['reply_to']) or any(r['reply_to']==row['origin'] for r in neighbours)
    own=any(r['user_id']=='SELF' and r['origin']==row['reply_to'] for r in neighbours)
    multi=len({r['user_id'] for r in neighbours})>1
    text=row['text'];size=len(text.strip())
    value=0.8+FRESHNESS_WEIGHT*math.exp(-age/3)
    value+=INVOLVEMENT_WEIGHT*bool(involved(row) or own)
    value+=EXPLICIT_REPLY_WEIGHT if explicit else MULTI_SPEAKER_WEIGHT*multi
    value+=QUESTION_WEIGHT*bool(re.search(r'[?？]|为什么|怎样|如何|原来|因为|所以',text))
    value+=INFORMATION_WEIGHT*min(1,size/80)
    tokens=terms(text,limit=1200)
    similarity=max((len(tokens&t)/math.sqrt(len(tokens)*len(t)) for t in recent_terms if tokens and t),default=0)
    return value*max(0.35,1-0.8*similarity)


def choose(candidates, day):
    """Keep one strongest anchor, then seeded weighted draws without replacement."""
    remaining=sorted(candidates,key=lambda c:(-c['score'],-c['row']['at'],c['row']['id']))
    chosen=[];rng=random.Random(seed_for_day(day))
    while remaining and len(chosen)<MAX_GROUPS:
        if not chosen:
            candidate=remaining[0]
        else:
            weights=[]
            for c in remaining:
                row=c['row'];weight=c['score']**2
                if any(row['session']==x['row']['session'] for x in chosen):weight*=0.65
                if any(row['user_id']==x['row']['user_id'] for x in chosen):weight*=0.5
                weights.append(weight)
            candidate=rng.choices(remaining,weights=weights,k=1)[0]
        chosen.append(candidate);row=candidate['row']
        remaining=[c for c in remaining if c['row']['id']!=row['id']
                   and not (c['row']['session']==row['session'] and abs(c['row']['at']-row['at'])<=600)]
    return chosen


def material(root, day):
    database=root/'official_test_runtime/state/memory.sqlite3'
    if not database.is_file():
        logging.warning('diary_material: missing_runtime_database')
        return []
    try:
        day_date=datetime.fromisoformat(day).date()
        day_start=datetime.combine(day_date,walltime(),ZONE).timestamp()
        end=datetime.combine(day_date+timedelta(days=1),walltime(),ZONE).timestamp()
        now=min(time.time(),end)
        recent=[terms(r['text'],limit=6000) for r in entries(root,until=day)[-7:] if r['day']<day]
        bili_rows=[]
        result=[];used=set();budget=TEXT_BUDGET
        with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3) as db:
            db.row_factory=sqlite3.Row
            cols={r[1] for r in db.execute('PRAGMA table_info(records)')}
            has_nickname='nickname' in cols
            has_key='key' in cols
            has_channel='channel' in cols
            has_graph_aliases=bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_aliases'").fetchone())
            row_fields=FIELDS_WITH_NICKNAME if has_nickname else FIELDS
            rows=candidate_rows(db,day,now,fields=row_fields);seen=set();candidates=[]
            for row in rows:
                text=row['text'].strip();key=normalize(text)
                if len(key)<12 or key in seen or text.startswith(('[图片','[表情','[CQ:')) or re.fullmatch(r'https?://\S+',text):
                    continue
                seen.add(key);neighbours=context_rows(db,row,now,fields=row_fields)
                candidates.append({'row':row,'neighbours':neighbours,'score':score(row,neighbours,now,recent)})
            if has_channel:
                try:
                    bili_rows=db.execute(
                        "SELECT id,text,at FROM records WHERE session='self:bilibili' "
                        "AND channel='bilibili_watch' AND kind='episode' AND state='active' "
                        "AND (expires=0 OR expires>?) AND at>=? AND at<? "
                        "ORDER BY (expires=0 OR (expires-at)>2592000) DESC, at DESC, id DESC LIMIT 2",
                        (now,day_start,now),
                    ).fetchall()
                except sqlite3.Error:
                    bili_rows=[]
            bot_id=str(owner.bot_self_id() or '')
            for candidate in choose(candidates,day):
                anchor=candidate['row'];nearby=candidate['neighbours'];selected=[];cost=0
                for row in [anchor]+[r for r in nearby if r['id']!=anchor['id']]:
                    size=min(len(row['text']),EXCERPT_LIMIT)
                    if row['id'] not in used and cost+size<=min(GROUP_BUDGET,budget):
                        selected.append(row);cost+=size
                ordered=sorted(selected,key=lambda r:(r['at'],r['id']))
                if not ordered:
                    continue
                primary_map,speaker_aliases=_resolve_group_names(
                    db,ordered,anchor['session'],now,has_nickname,has_key,has_graph_aliases
                )
                origins={r['origin']:'M'+str(r['id']) for r in ordered};messages=[]
                for row in ordered:
                    role='自己' if row['user_id']=='SELF' else primary_map.get(str(row['user_id']),'群友1')
                    row_self_id=str(row['self_id'] or '').strip()
                    cleaned_text=_clean_cq_mentions(row['text'],row_self_id,bot_id,primary_map)
                    messages.append({'source':'M'+str(row['id']),
                        'received_at':datetime.fromtimestamp(row['at'],ZONE).isoformat(timespec='seconds'),
                        'speaker':role,'text':cleaned_text[:EXCERPT_LIMIT],
                        'reply_to_source':origins.get(row['reply_to']), 'excerpted':len(cleaned_text)>EXCERPT_LIMIT})
                    used.add(row['id'])
                if messages:
                    group_item={'date':datetime.fromtimestamp(anchor['at'],ZONE).date().isoformat(),
                        'scope':'公开群原文；speaker为群昵称/称呼（缺省时才用群友编号）；按实际收件时刻排列，间隔见时间；收件时间不等于回忆中事件发生时间',
                        'messages':messages}
                    if speaker_aliases:
                        group_item['speaker_aliases']=speaker_aliases
                    result.append(group_item)
                    budget-=cost
        if bili_rows and budget>=80:
            bili_msgs=[]
            for brow in bili_rows:
                btext=str(brow['text'] or '').strip()[:EXCERPT_LIMIT]
                if not btext or len(btext)>budget:
                    continue
                bili_msgs.append({
                    'source':'M'+str(brow['id']),
                    'received_at':datetime.fromtimestamp(brow['at'],ZONE).isoformat(timespec='seconds'),
                    'speaker':'自己',
                    'text':btext,
                    'reply_to_source':None,
                    'excerpted':len(str(brow['text'] or '').strip())>EXCERPT_LIMIT,
                })
                budget-=len(btext)
            if bili_msgs:
                result.append({
                    'date':day,
                    'scope':'B站亲历（自己当日在B站观看的视频或直播经历）',
                    'messages':bili_msgs,
                })
        return result
    except (OSError,sqlite3.Error,ValueError) as exc:
        logging.warning('diary_material: %s',type(exc).__name__)
        return []
