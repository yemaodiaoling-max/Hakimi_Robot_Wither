"""Prepare/apply/rollback a two-file QQ hook, with hashes and exact anchors.

Run on the server using its existing hakimi venv. Never restarts a service.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time

ROOT = Path('/opt/hakimi/app/src/hakimi')
TARGET = ROOT / 'prompt/prompt_context.py'
HELPER = ROOT / 'memory/bridge_context.py'
ANCHOR = '    return context\n\n\ndef _persona_section'
HOOK = '''    # BILIBILI_MEMORY_BRIDGE_V1: independently sourced, bounded data.
    from hakimi.memory.bridge_context import context_for as bridge_context_for
    context.update(bridge_context_for(base, str(target.get('text', '')), now))
    return context


def _persona_section'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def prepare(destination):
    import hakimi.prompt.prompt_context as module
    if Path(module.__file__).resolve() != TARGET:
        raise ValueError('installed QQ prompt path differs from reviewed path')
    source = TARGET.read_text(encoding='utf-8')
    if 'BILIBILI_MEMORY_BRIDGE_V1' in source or source.count(ANCHOR) != 1:
        raise ValueError('hook exists or source anchor changed; review required')
    stage = Path(destination).resolve()
    stage.mkdir(parents=True, exist_ok=False)
    helper_source = Path(__file__).resolve().parents[1]/'src/bili_companion/integrations/qq_context.py'
    (stage/'prompt_context.py.new').write_text(source.replace(ANCHOR, HOOK), encoding='utf-8')
    shutil.copy2(helper_source, stage/'bridge_context.py.new')
    compile((stage/'prompt_context.py.new').read_text(encoding='utf-8'),str(TARGET),'exec')
    compile((stage/'bridge_context.py.new').read_text(encoding='utf-8'),str(HELPER),'exec')
    shutil.copy2(TARGET, stage/'prompt_context.py.before')
    if HELPER.exists():
        shutil.copy2(HELPER, stage/'bridge_context.py.before')
    manifest = {'created_at':time.time(),'target_before':sha(TARGET),'helper_before':sha(HELPER),
        'target_after':sha(stage/'prompt_context.py.new'),'helper_after':sha(stage/'bridge_context.py.new')}
    (stage/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps({'state':'prepared','stage':str(stage),'files':2,'restart_required':'hakimi-agent.service'}))


def activate(stage, rollback=False):
    stage=Path(stage).resolve()
    manifest=json.loads((stage/'manifest.json').read_text(encoding='utf-8'))
    before, after = ('after','before') if rollback else ('before','after')
    if sha(TARGET) != manifest['target_'+before] or sha(HELPER) != manifest['helper_'+before]:
        raise ValueError('live files changed since preparation; refusing overwrite')
    suffix='before' if rollback else 'new'
    operations = [(HELPER,'bridge_context.py','helper'),(TARGET,'prompt_context.py','target')]
    if rollback:
        operations.reverse()
    for destination,name,key in operations:
        source=stage/(name+'.'+suffix)
        if manifest[key+'_'+after] is not None and sha(source) != manifest[key+'_'+after]:
            raise ValueError('prepared file checksum mismatch')
    originals={path:path.read_bytes() if path.exists() else None for path,_,_ in operations}
    changed=[]
    try:
        for destination,name,key in operations:
            desired=manifest[key+'_'+after]
            if desired is None:
                destination.unlink(missing_ok=True)
            else:
                source=stage/(name+'.'+suffix)
                temp=destination.with_name(destination.name+'.bridge-tmp')
                shutil.copy2(source,temp)
                temp.replace(destination)
            changed.append(destination)
    except BaseException:
        for destination in reversed(changed):
            content=originals[destination]
            if content is None:
                destination.unlink(missing_ok=True)
            else:
                temp=destination.with_name(destination.name+'.bridge-restore')
                temp.write_bytes(content)
                temp.replace(destination)
        raise
    print(json.dumps({'state':'rolled_back' if rollback else 'applied','restart_required':'hakimi-agent.service'}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','apply','rollback'))
    parser.add_argument('--stage',required=True)
    args=parser.parse_args()
    if args.action=='prepare':
        prepare(args.stage)
    else:
        activate(args.stage,rollback=args.action=='rollback')


if __name__=='__main__':
    main()
