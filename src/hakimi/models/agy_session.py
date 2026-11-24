"""One native streaming conversation, retained until an actual failure."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time


class AgySession:
    def __init__(self,args,cwd,env,*,state_path=None,timeout=450,resume=False):
        self.args,self.cwd,self.env=args,cwd,env
        self.state_path=Path(state_path) if state_path else None
        self.timeout=timeout
        self.process=None
        self.selector=None
        self.pending=b''
        self.usage={}
        self.conversation_id=None
        self.turns=0
        self.resume=None
        self.usage_unknown=False
        self.context_estimate=0
        if resume and self.state_path:
            try:
                previous=json.loads(self.state_path.read_text(encoding='utf-8'))
                if previous.get('status')=='ready' and previous.get('conversation_id'):
                    self.resume=previous
            except (OSError,ValueError):pass

    def note(self,status):
        if self.state_path:
            value={'at':time.time(),'status':status,'mode':'persistent',
                   'pid':self.process.pid if self.process else None,
                   'conversation_id':self.conversation_id,'turns':self.turns,'usage':self.usage,
                   'context_estimate':self.context_estimate,'context_estimate_version':1,
                   'model':self.args[self.args.index('--model')+1] if '--model' in self.args else None}
            self.state_path.parent.mkdir(parents=True,exist_ok=True)
            temp=self.state_path.with_suffix('.tmp')
            temp.write_text(json.dumps(value),encoding='utf-8');os.replace(temp,self.state_path)

    def start(self):
        if self.process and self.process.poll() is None:return
        self.close()
        self.pending=b'';self.usage={};self.conversation_id=None;self.turns=0;self.context_estimate=0
        args=list(self.args)
        previous,self.resume=self.resume,None
        self.usage_unknown=False
        if previous:
            self.conversation_id=previous['conversation_id'];self.turns=previous.get('turns',0)
            self.usage=previous.get('usage',{});self.usage_unknown='usage' not in previous
            self.context_estimate=previous.get('context_estimate',0)
            args+=['--conversation',self.conversation_id]
        self.process=subprocess.Popen(args,cwd=self.cwd,env=self.env,stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
        self.selector=selectors.DefaultSelector()
        self.selector.register(self.process.stdout,selectors.EVENT_READ)
        self.note('ready')

    def accept(self,result):
        if not isinstance(result,dict):raise RuntimeError('model_result_invalid')
        cid=result.get('conversation_id')
        if self.conversation_id and cid and cid!=self.conversation_id:
            raise RuntimeError('model_conversation_changed')
        self.conversation_id=cid or self.conversation_id
        self.turns=result.get('num_turns',self.turns+1)
        cumulative=result.get('usage') or {}
        # Native streaming counters are cumulative; activity logs stay per-turn.
        usage={key:max(0,value-self.usage.get(key,0)) for key,value in cumulative.items()
               if type(value) is int and value>=0}
        if self.usage_unknown:usage={};self.usage_unknown=False
        self.usage={key:value for key,value in cumulative.items() if type(value) is int and value>=0}
        return dict(result,usage=usage)

    def generate(self,prompt,coordinator,claim,*,timeout=None):
        # Optional per-turn ceiling.  Normal QQ turns leave it unset and keep the
        # configured session timeout; internal reflection may pass <=90s.
        limit=self.timeout if timeout is None else timeout
        if isinstance(limit,bool) or not isinstance(limit,(int,float)) or not limit>0:
            raise ValueError('turn timeout must be a positive number')
        try:
            self.start()
            from hakimi.prompt.prompt_budget import estimate_tokens, positive_setting
            # Account for submitted text plus unseen native tool exchanges.
            # This ledger is deliberately separate from cumulative billing usage.
            self.context_estimate += estimate_tokens(prompt) + positive_setting(self.env,'HAKIMI_CONTEXT_TOOL_RESERVE',4096,1024,16000)
            self.note('running')
            self.process.stdin.write((json.dumps({'event':'user','message':{'content':prompt}})+'\n').encode())
            self.process.stdin.flush()  # Leave stdin open for the next turn.
            deadline=time.monotonic()+float(limit)
            while time.monotonic()<deadline:
                while b'\n' in self.pending:
                    raw,self.pending=self.pending.split(b'\n',1)
                    try:event=json.loads(raw)
                    except ValueError:continue
                    step=event.get('step_update',{})
                    if event.get('event')=='step_update' and step.get('step_type') in {'tool','agent_response'}:
                        coordinator.record_progress(claim['token'])
                    if event.get('event')=='result':
                        result=self.accept(event.get('result'))
                        self.context_estimate += estimate_tokens(str(result.get('response','')))
                        if result.get('status')=='SUCCESS':self.note('ready')
                        else:self.close('error')
                        return result
                remaining=deadline-time.monotonic()
                if remaining<=0:break
                for key,_ in self.selector.select(timeout=min(1,remaining)):
                    chunk=os.read(key.fd,65536)
                    if not chunk:raise RuntimeError('model_session_ended')
                    self.pending+=chunk
                    if len(self.pending)>2*1024*1024:raise RuntimeError('model_output_limit')
            raise TimeoutError('model_response_timeout')
        except BaseException:
            self.close('error')
            raise

    def close(self,status='closed'):
        if self.process:
            try:os.killpg(self.process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            self.process.wait()
            for pipe in (self.process.stdin,self.process.stdout):
                try:pipe.close()
                except (OSError,ValueError):pass
            self.note(status)
            self.process=None
        if self.selector:self.selector.close();self.selector=None
