"""Typed, single-level child dispatch. Models request roles, never tools or URLs."""
from dataclasses import dataclass
from typing import Literal

from .gateway import CognitionUnavailable


@dataclass(frozen=True)
class RoleTask:
    role: Literal['expression','memory']
    reason: str
    evidence_indices: tuple[int,...]

    def __post_init__(self):
        if (self.role not in {'expression','memory'} or type(self.reason) is not str or len(self.reason)>80
                or type(self.evidence_indices) is not tuple or not 1<=len(self.evidence_indices)<=3
                or any(type(i) is not int or i<0 for i in self.evidence_indices)):
            raise ValueError('invalid typed role task')

    @classmethod
    def parse(cls,value,count):
        if (type(value) is not dict or set(value)!={'role','reason','evidence_indices'}
                or value['role'] not in {'expression','memory'} or type(value['reason']) is not str
                or len(value['reason'])>80 or type(value['evidence_indices']) is not list
                or not 1<=len(value['evidence_indices'])<=3
                or any(type(i) is not int or not 0<=i<count for i in value['evidence_indices'])):
            raise ValueError('invalid bounded role task')
        return cls(value['role'],value['reason'],tuple(dict.fromkeys(value['evidence_indices'])))


@dataclass(frozen=True)
class RoleResult:
    role: str
    status: str
    request_id: str|None=None
    usage: dict|None=None
    value: dict|None=None


class RoleDispatcher:
    def __init__(self,engine):
        self.engine=engine
        with engine.ledger.database.connect(write=True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS cognition_child_rounds (
                parent_id TEXT PRIMARY KEY,role TEXT,epoch INTEGER,status TEXT)''')

    def dispatch(self,task,*,observation,scope,session,epoch,parent_id,depth=0):
        engine=self.engine
        if type(task) is not RoleTask or depth!=0:return RoleResult('unknown','denied')
        if not observation.evidence:return RoleResult(task.role,'no_evidence')
        if any(i>=len(observation.evidence) for i in task.evidence_indices):return RoleResult(task.role,'invalid_reference')
        cited=[observation.evidence[i] for i in task.evidence_indices]
        if any(e.attribution!='observation' or e.confidence<.8 for e in cited):
            return RoleResult(task.role,'low_confidence')
        if task.role=='expression' and any(e.observed_at+(60 if scope.kind=='live_room' else 300)<=engine.clock() for e in cited):
            return RoleResult(task.role,'expired')
        def active():
            if task.role=='expression' and any(e.observed_at+(60 if scope.kind=='live_room' else 300)<=engine.clock() for e in cited):
                return False
            checker=engine.epoch_is_current
            return checker is not None and checker(scope,session,epoch) is True
        if not active():return RoleResult(task.role,'stale_or_unbound_epoch')
        with engine.ledger.database.connect(write=True) as db:
            if not db.execute("INSERT OR IGNORE INTO cognition_child_rounds VALUES (?,?,?,'claimed')",
                (parent_id,task.role,epoch)).rowcount:return RoleResult(task.role,'round_limit')
        bundle=engine.persona.load(engine.bridge.recall(budget=1000) if engine.bridge else ())
        if task.role=='expression':
            maximum=30 if scope.kind=='live_room' else 120
            instruction=(bundle.system_instruction+'\n你是一次性表达子代理。仅根据给定实际观察决定是否自然表达；不冒称看完整片。'
                '直接抓具体细节随手接一句短话，省去画面复述、AI套话与机械耳朵尾巴收尾。'
                '只返回JSON {"speak":false,"text":"","evidence_indices":[]}。'
                f'最多{maximum}字，只能引用给定原始事件索引；不调用工具、不请求子代理。')
        else:
            instruction=(bundle.system_instruction+'\n你是一次性记忆整理子代理。只整理给定实际观察，保留听到/看到和片段范围，'
                '只返回JSON {"summary":"最多300字","evidence_indices":[0]}。'
                '只能引用给定原始事件索引；不补未观察内容，不调用工具、不请求子代理。')
        data=dict(events=[dict(index=i,text=observation.evidence[i].text,
            modality=observation.evidence[i].modality,start=observation.evidence[i].media_start,
            end=observation.evidence[i].media_end) for i in task.evidence_indices],task_reason=task.reason)
        identity=None;usage=None
        try:
            purpose=('video-' if scope.kind=='video' else 'live-')+'child-'+task.role
            response,identity,usage=engine._call(engine.observer_model,engine._payload(instruction,data),
                purpose=purpose,role=task.role,parent_id=parent_id,epoch_guard=active)
            from .live import _json_response
            value=_json_response(response)
            expected={'speak','text','evidence_indices'} if task.role=='expression' else {'summary','evidence_indices'}
            if set(value)!=expected:raise ValueError('child output includes unsupported fields')
            indices=value['evidence_indices']
            if type(indices) is not list or len(indices)>3 or any(type(i) is not int or i not in task.evidence_indices for i in indices):
                raise ValueError('child reference outside allowed facts')
            if task.role=='expression':
                if type(value['speak']) is not bool or type(value['text']) is not str or len(value['text'])>maximum:
                    raise ValueError('invalid child expression')
                if value['speak'] and (not value['text'].strip() or not indices):raise ValueError('ungrounded expression')
            elif type(value['summary']) is not str or len(value['summary'])>300 or value['summary'].strip() and not indices:
                raise ValueError('ungrounded memory')
            result=RoleResult(task.role,'completed',identity,usage,value) if active() else RoleResult(task.role,'stale_result',identity,usage)
        except Exception as exc:
            ids=getattr(exc,'request_ids',())
            if ids:identity=ids[0]
            result=RoleResult(task.role,'budget_paused' if type(exc).__name__=='BudgetPaused' else 'unavailable_or_invalid',identity,usage)
        with engine.ledger.database.connect(write=True) as db:
            db.execute('UPDATE cognition_child_rounds SET status=? WHERE parent_id=?',(result.status,parent_id))
        return result
