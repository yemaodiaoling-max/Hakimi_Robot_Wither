"""Small, hot-read official release anchor; updated by the diary's Terra pass."""
from hakimi import paths
from datetime import date,datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import argparse,base64,json,re,time

BASELINE={'version':'7.1','name':'往冥府的安魂歌','source':'https://ys.mihoyo.com/main/m/news/detail/166262','released_on':'2026-09-23'}

def number(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{1,3}\.\d{1,3}',value):raise ValueError('invalid_version')
    return tuple(map(int,value.split('.')))

def path(base):
    base=Path(base).resolve()
    return (base.parent if base.name=='official_test_runtime' else base)/'state/genshin_version.json'

def current(base):
    try:
        value=json.loads(path(base).read_text(encoding='utf-8'))
        if number(value['version'])>=number(BASELINE['version']) and isinstance(value.get('name'),str) and value['name'].strip():return value
    except (OSError,ValueError,KeyError,TypeError):pass
    return dict(BASELINE)

def update(base,candidate,*,today=None):
    import fcntl
    today=today or datetime.now(ZoneInfo('America/New_York')).date()
    version=candidate['version'];name=candidate['name'].strip()
    number(version)
    if not name or len(name)>100 or any(c in name for c in '\n\r'):raise ValueError('invalid_version_name')
    if not str(candidate.get('source','')).startswith('https://') or not str(candidate.get('evidence','')).strip():raise ValueError('official_evidence_required')
    if date.fromisoformat(candidate['released_on'])>today:raise ValueError('release_not_live')
    target=path(base);target.parent.mkdir(parents=True,exist_ok=True)
    with target.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        previous=current(base)
        if number(version)<=number(previous['version']):return {'status':'kept','current':previous}
        value={key:candidate[key] for key in ('version','name','source','evidence','released_on')}
        value['checked_at']=time.time()
        temp=target.with_suffix('.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(target)
        return {'status':'updated','current':value}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--payload');args=parser.parse_args()
    base=paths.base()
    result=update(base,json.loads(base64.b64decode(args.payload))) if args.payload else {'status':'current','current':current(base)}
    print(json.dumps(result,ensure_ascii=False))
