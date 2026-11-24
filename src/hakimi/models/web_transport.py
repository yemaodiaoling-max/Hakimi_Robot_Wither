"""Bound every network operation, including DNS, by a killable process deadline."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from hakimi import paths


def request(kind,args,timeout):
    if timeout<.1:return {'error':'web_deadline_exhausted','billable':None}
    env=dict(os.environ)
    for key in list(env):
        if key.startswith('AGY_LLM_GATEWAY_'):env.pop(key,None)
    env['PYTHONPATH']=str(Path(__file__).resolve().parents[2])
    env['PATH']=str(paths.dsh_bin())+os.pathsep+env.get('PATH','')
    options={'start_new_session':True} if os.name!='nt' else {'creationflags':getattr(subprocess,'CREATE_NO_WINDOW',0)}
    try:
        process=subprocess.Popen([sys.executable,'-m','hakimi.models.web_fetch_worker'],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,**options)
    except OSError:return {'error':'web_worker_unavailable','billable':False}
    try:
        output,_=process.communicate(json.dumps({'kind':kind,'args':args,'timeout':timeout}).encode(),timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name!='nt':os.killpg(process.pid,signal.SIGKILL)
            else:process.kill()
        except ProcessLookupError:pass
        process.communicate()
        return {'error':'web_deadline_exhausted','billable':None}
    if process.returncode or len(output)>4_500_000:return {'error':'web_worker_failed','billable':None}
    try:return json.loads(output)
    except ValueError:return {'error':'web_worker_invalid_result','billable':None}
