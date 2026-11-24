"""One live native process, resumable per-group conversations, bounded stale context."""
import hashlib
import json
import os
import re
import time
from hakimi.prompt.prompt_budget import estimate_tokens, positive_setting, segment_estimates
from pathlib import Path


class ScopedSession:
    def __init__(self, args, cwd, env, state_dir, factory=None):
        if factory is None:
            from hakimi.models.agy_session import AgySession
            factory=AgySession
        self.args,self.cwd,self.env=args,cwd,env
        self.state_dir=Path(state_dir);self.factory=factory
        self.current=None;self.key=None

    def generate(self,prompt,coordinator,claim,*,timeout=None):
        key=claim.get('payload',{}).get('session_key','')
        if not re.fullmatch(r'(?:group|private):[1-9][0-9]{0,19}',key):raise ValueError('invalid_session_key')
        lane = str(self.env.get('HAKIMI_TURN_LANE', ''))
        scoped_key = key + ('\nturn_lane:' + lane if lane else '')
        if scoped_key!=self.key:
            self.close('ready')  # Persist this group's native conversation checkpoint.
            self.state_dir.mkdir(parents=True,exist_ok=True)
            target=self.state_dir/(hashlib.sha256(scoped_key.encode()).hexdigest()[:24]+'.json')
            self.current=self.factory(self.args,self.cwd,self.env,state_path=target,resume=True)
            self.key=scoped_key
        # Full prompts remain intact on every turn: tool authority and dynamic
        # memory opportunities cannot disappear on resume or failure recovery.
        previous=self.current.resume or {}
        turns=max(self.current.turns,previous.get('turns',0))
        stored=previous.get('context_estimate',0)
        history=max(getattr(self.current,'context_estimate',0),stored if type(stored) is int and stored>=0 else 0)
        added=estimate_tokens(prompt)
        reserve=positive_setting(self.env,'HAKIMI_CONTEXT_TOOL_RESERVE',4096,1024,16000)
        budget=positive_setting(self.env,'HAKIMI_CONTEXT_TOKEN_BUDGET',24000,8000,64000)
        max_turns=positive_setting(self.env,'HAKIMI_CONTEXT_MAX_TURNS',4,1,8)
        reason=None
        if previous and (previous.get('context_estimate_version')!=1
                         or type(previous.get('context_estimate')) is not int
                         or previous['context_estimate']<0):
            reason='legacy_or_unknown_history'
        elif turns>=max_turns:reason='turn_limit'
        elif turns and history+added+reserve>budget:reason='estimated_context_budget'
        if reason:
            self.current.close('rotated');self.current.resume=None
            # Discard the closed object's counters; start() resets native history.
            self.current.turns=0;self.current.context_estimate=0
        audit={'at':time.time(),'scope_hash':self.current.state_path.stem,
               'prompt_chars':len(prompt),'prompt_estimated_tokens':added,
               'history_estimated_tokens':history,'tool_reserve_estimated_tokens':reserve,
               'budget_estimated_tokens':budget,'rotation_reason':reason,
               'single_prompt_over_budget':added+reserve>budget,
               'estimate_only':True,'segments':segment_estimates(prompt)}
        # One bounded last-event file per scope, no message or credential text.
        audit_path=self.current.state_path.with_suffix('.budget.json')
        audit_temp=audit_path.with_suffix('.tmp')
        try:
            audit_temp.write_text(json.dumps(audit),encoding='utf-8');os.replace(audit_temp,audit_path)
        except OSError:
            pass  # Optional telemetry must not prevent a normal model reply.
        try:
            if timeout is None:
                return self.current.generate(prompt,coordinator,claim)
            return self.current.generate(prompt,coordinator,claim,timeout=timeout)
        finally:
            # Keep the existing read-only activity view compatible, without sharing context.
            if self.current.state_path.exists():
                value=json.loads(self.current.state_path.read_text())
                value['mode']='per_session';value['scope_hash']=self.current.state_path.stem
                target=self.state_dir.parent/('gemini_session_'+lane+'.json' if lane else 'gemini_session.json')
                tmp=target.with_suffix('.'+str(os.getpid())+'.tmp')
                tmp.write_text(json.dumps(value));os.replace(tmp,target)

    def close(self,status='ready'):
        if self.current:self.current.close(status)
        self.current=None;self.key=None
