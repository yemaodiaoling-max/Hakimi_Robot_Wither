"""Bounded, local weighted diary selection; no model calls or database writes."""
from datetime import datetime, time as walltime, timedelta
import json
import logging
import math
import random
import re
import sqlite3
import time

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
VALID = "kind='chat' AND state='active' AND session LIKE 'group:%' AND origin!='' AND text!='' AND (expires=0 OR expires>?)"


def normalize(text):
    return re.sub(r'\s+|[^\w\u4e00-\u9fff]', '', text.lower())


def seed_for_day(day):
    return 'diary:'+day


def candidate_rows(db, day, now):
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
        result.extend(db.execute('SELECT '+FIELDS+' FROM records WHERE '+VALID+
            ' AND at>=? AND at<? ORDER BY at DESC,id DESC LIMIT ?',
            (now,start.timestamp(),until,limit)).fetchall())
    return result


def involved(row):
    try: targets=json.loads(row['direct_targets'] or '[]')
    except (ValueError,TypeError): targets=[]
    return row['user_id']=='SELF' or bool(row['self_id'] and isinstance(targets,list)
        and str(row['self_id']) in [str(t) for t in targets])


def context_rows(db, anchor, now):
    """Independently fetch actual neighbours, rather than joining sampled buckets."""
    params=(now,anchor['session'])
    before=db.execute('SELECT '+FIELDS+' FROM records WHERE '+VALID+
        ' AND session=? AND at>=? AND (at<? OR (at=? AND id<?)) ORDER BY at DESC,id DESC LIMIT 2',
        (*params,anchor['at']-300,anchor['at'],anchor['at'],anchor['id'])).fetchall()
    after=db.execute('SELECT '+FIELDS+' FROM records WHERE '+VALID+
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
        with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3) as db:
            db.row_factory=sqlite3.Row
            rows=candidate_rows(db,day,now);seen=set();candidates=[]
            for row in rows:
                text=row['text'].strip();key=normalize(text)
                if len(key)<12 or key in seen or text.startswith(('[图片','[表情','[CQ:')) or re.fullmatch(r'https?://\S+',text):
                    continue
                seen.add(key);neighbours=context_rows(db,row,now)
                candidates.append({'row':row,'neighbours':neighbours,'score':score(row,neighbours,now,recent)})
            try:
                cols={r[1] for r in db.execute('PRAGMA table_info(records)')}
                if 'channel' in cols:
                    bili_rows=db.execute(
                        "SELECT id,text,at FROM records WHERE session='self:bilibili' "
                        "AND channel='bilibili_watch' AND kind='episode' AND state='active' "
                        "AND (expires=0 OR expires>?) AND at>=? AND at<? "
                        "ORDER BY (expires=0 OR (expires-at)>2592000) DESC, at DESC, id DESC LIMIT 2",
                        (now,day_start,now),
                    ).fetchall()
            except sqlite3.Error:
                bili_rows=[]
        result=[];used=set();budget=TEXT_BUDGET
        for candidate in choose(candidates,day):
            anchor=candidate['row'];nearby=candidate['neighbours'];selected=[];cost=0
            for row in [anchor]+[r for r in nearby if r['id']!=anchor['id']]:
                size=min(len(row['text']),EXCERPT_LIMIT)
                if row['id'] not in used and cost+size<=min(GROUP_BUDGET,budget):
                    selected.append(row);cost+=size
            speakers={};origins={r['origin']:'M'+str(r['id']) for r in selected};messages=[]
            for row in sorted(selected,key=lambda r:(r['at'],r['id'])):
                role='自己' if row['user_id']=='SELF' else speakers.setdefault(row['user_id'],'群友'+str(len(speakers)+1))
                messages.append({'source':'M'+str(row['id']),
                    'received_at':datetime.fromtimestamp(row['at'],ZONE).isoformat(timespec='seconds'),
                    'speaker':role,'text':row['text'][:EXCERPT_LIMIT],
                    'reply_to_source':origins.get(row['reply_to']), 'excerpted':len(row['text'])>EXCERPT_LIMIT})
                used.add(row['id'])
            if messages:
                result.append({'date':datetime.fromtimestamp(anchor['at'],ZONE).date().isoformat(),
                    'scope':'公开群原文；群友编号仅本组有效；按实际收件时刻排列，间隔见时间；收件时间不等于回忆中事件发生时间',
                    'messages':messages})
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
