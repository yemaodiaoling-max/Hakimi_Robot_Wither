"""Bound every network operation, including DNS, by a killable process deadline."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from hakimi import paths
from hakimi.core.process_job import WindowsJob


def _dsh_job_error(code):
    from hakimi.models.fallback_runner import FallbackError

    return FallbackError('FALLBACK_' + code)


def request(kind,args,timeout):
    if timeout<.1:return {'error':'web_deadline_exhausted','billable':None}
    if kind=='dsh_search':
        with tempfile.TemporaryDirectory(prefix='hakimi-search-') as runtime:
            return _request(kind,dict(args,runtime=runtime),timeout)
    return _request(kind,args,timeout)


def _request(kind,args,timeout):
    env=dict(os.environ)
    for key in list(env):
        if key.startswith('AGY_LLM_GATEWAY_'):env.pop(key,None)
    env['PYTHONPATH']=str(Path(__file__).resolve().parents[2])
    env['PATH']=str(paths.dsh_bin())+os.pathsep+env.get('PATH','')
    options={'start_new_session':True} if os.name!='nt' else {'creationflags':getattr(subprocess,'CREATE_NO_WINDOW',0)}
    try:
        process=subprocess.Popen([sys.executable,'-m','hakimi.web.web_fetch_worker'],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,**options)
    except OSError:return {'error':'web_worker_unavailable','billable':False}
    job=None
    try:
        if os.name=='nt' and kind=='dsh_search':
            try:
                job=WindowsJob(process,error_factory=_dsh_job_error)
            except Exception:
                process.kill()
                process.communicate()
                raise
        output,_=process.communicate(json.dumps({'kind':kind,'args':args,'timeout':timeout}).encode(),timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name!='nt':os.killpg(process.pid,signal.SIGKILL)
            else:process.kill()
        except ProcessLookupError:pass
        process.communicate()
        return {'error':'web_deadline_exhausted','billable':None}
    finally:
        if job:job.close()
    if process.returncode or len(output)>4_500_000:return {'error':'web_worker_failed','billable':None}
    try:return json.loads(output)
    except ValueError:return {'error':'web_worker_invalid_result','billable':None}
