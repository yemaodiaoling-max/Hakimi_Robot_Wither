"""Model trial state file helpers (read without starting or reviewing a trial)."""
import json
from pathlib import Path


def root(base):
    p=Path(base).resolve();return p.parent if p.name=='official_test_runtime' else p


def read(path,default=None):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):return {} if default is None else default


def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2));temp.replace(path)


def state(base):return read(root(base)/'state/model_trial.json')
