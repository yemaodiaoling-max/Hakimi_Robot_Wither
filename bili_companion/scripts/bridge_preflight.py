"""Read-only, content-free QQ idle check. Not a race-free pause mechanism."""
import argparse
import json
from pathlib import Path


def inspect(base):
    state=Path(base)/'state'
    active=[]
    errors=0
    paths=[state/'turn_state.json',*[state/'turn_lanes'/f'{lane}.json' for lane in ('at1','at2','at3')]]
    for path in paths:
        if not path.exists():
            continue
        try:
            row=json.loads(path.read_text(encoding='utf-8'))
            if row.get('status') in {'queued','primary_active','fallback_active'} or row.get('reply_intent'):
                active.append({'lane':'main' if path.name=='turn_state.json' else path.stem,
                               'generation_active':row.get('status') in {'primary_active','fallback_active'},
                               'queued':row.get('status')=='queued','reply_intent':bool(row.get('reply_intent'))})
        except (OSError,ValueError,TypeError):
            errors+=1
    delivery=state/'delivery_state.json'
    pending=False
    if delivery.exists():
        try:
            pending=bool(json.loads(delivery.read_text(encoding='utf-8')).get('pending'))
        except (OSError,ValueError,TypeError):
            errors+=1
    return {'lanes_busy':active,'delivery_pending':pending,'read_errors':errors,
            'idle_observed':not active and not pending and errors==0,
            'restart_target':'hakimi-agent.service','worker_untouched':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',default='/opt/hakimi/data/official_test_runtime')
    args=parser.parse_args()
    result=inspect(args.base)
    print(json.dumps(result))
    raise SystemExit(0 if result['idle_observed'] else 2)
