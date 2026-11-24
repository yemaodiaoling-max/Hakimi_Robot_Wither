"""Operator-only, bounded activity views. Never sends a QQ message."""
from hakimi import paths
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import time
from zoneinfo import ZoneInfo

ROOT=paths.root()
BASE=ROOT/'official_test_runtime'


def load(path,default=None):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):return default


def tail(path,count=20):
    try:
        with path.open('rb') as stream:
            stream.seek(0,2);start=max(0,stream.tell()-512*1024);stream.seek(start);raw=stream.read()
        if start:raw=raw.partition(b'\n')[2]
    except OSError:return []
    rows=[]
    for line in raw.decode('utf-8',errors='replace').splitlines():
        try:rows.append(json.loads(line))
        except ValueError:pass
    return rows[-count:]


def stamp(at):
    return datetime.fromtimestamp(at,ZoneInfo('America/New_York')).strftime('%m-%d %H:%M:%S')


def status():
    from hakimi.models.provider_health import availability
    worker=load(BASE/'state/official_worker.json',{})
    from hakimi.bridge.turn_lanes import LANES
    turns={lane or 'main':load(BASE/'state'/('turn_lanes/'+lane+'.json' if lane else 'turn_state.json'),{}) for lane in LANES}
    turn=turns['main']
    return {'host':'cloud','online':bool(worker.get('online') and time.time()-worker.get('at',0)<30),
            'groups':worker.get('groups',0),'private_sessions':worker.get('private_sessions',0),
            'last_private_received_at':worker.get('last_private_received_at'),'provider_health':availability(ROOT),
            'turn_status':turn.get('status','idle'),'turn_provider':turn.get('provider'),
            'turn_lanes':{lane:{key:value.get(key) for key in ('status','provider','trigger_id','expires_at')} for lane,value in turns.items()},
            'worker_pid':worker.get('pid'),'gemini_session':load(ROOT/'state/gemini_session.json',{})}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('view',choices=['status','groups','recent','turns','errors','supervisor','cooldowns','style'],nargs='?',default='status')
    parser.add_argument('--group',default='')
    parser.add_argument('--limit',type=int,default=20)
    args=parser.parse_args();args.limit=max(1,min(100,args.limit))
    groups=load(BASE/'group_profiles.json',{}).get('groups',{})
    weights=load(BASE/'config.json',{}).get('group_weights',{})
    def label(gid,row):return row.get('name') or row.get('group_name') or weights.get(gid,{}).get('name','')
    if args.view=='status':print(json.dumps(status(),ensure_ascii=False,indent=2));return
    if args.view=='groups':
        for gid,row in groups.items():print(gid,label(gid,row),row.get('profile',''))
        return
    if args.view=='supervisor':
        from hakimi.ops.cloud_supervisor import main as supervise
        import sys
        sys.argv=[sys.argv[0],'--report'];supervise();return
    if args.view=='cooldowns':
        from hakimi.bridge.interaction_policy import state
        rows=[{'user_id':uid,'minutes_left':round((row['until']-time.time())/60,1)}
              for uid,row in state(BASE).get('users',{}).items() if row.get('until',0)>time.time()]
        print(json.dumps(rows,ensure_ascii=False));return
    if args.view=='style':
        from hakimi.models.fallback_style import style_feedback
        print(style_feedback(BASE) or '尚未产生 dsh 专属风格建议。');return
    if args.view=='errors':
        result=subprocess.run(['journalctl','--user','-u','hakimi-agent.service','-n','100','--no-pager','-o','json'],
                              capture_output=True,text=True,timeout=10)
        rows=[]
        for line in result.stdout.splitlines():
            try:entry=json.loads(json.loads(line).get('MESSAGE',''))
            except (ValueError,TypeError):continue
            if entry.get('component')!='gemini':continue
            # Only our fixed logger fields; never emit raw journal text/prompts.
            row={k:entry[k] for k in ['at','component','status','error','reason','conversation_id','session_turn','usage'] if k in entry}
            rows.append(row)
        for row in rows[-args.limit:]:print(json.dumps(row,ensure_ascii=False))
        return
    sessions={args.group.removeprefix('group:')} if args.group else None
    if args.group and args.group in [label(gid,row) for gid,row in groups.items()]:
        sessions={gid for gid,row in groups.items() if label(gid,row)==args.group}
    filename='sent_messages.jsonl' if args.view=='recent' else 'turn_events.jsonl'
    rows=tail(BASE/'state'/filename,1000)
    rows=[row for row in rows if sessions is None or row.get('session_key','').removeprefix('group:') in sessions]
    for row in rows[-args.limit:]:
        at=row.get('at',row.get('completed_at',row.get('created_at',0)))
        detail=row.get('text','') if args.view=='recent' else row.get('wake_mode','')+' / '+row.get('outcome','')
        print(f"[{stamp(at)} 美东] {row.get('session_key','')} {row.get('provider','')} {detail}")
    if not rows:print('暂无相应记录；确认发送日志从本次更新开始积累。')


if __name__=='__main__':main()
