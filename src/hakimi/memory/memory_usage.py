"""Receipt diagnostics only; no query, note, ID, token or transcript is logged."""
TOOLS={'memory.read','memory.search','memory.graph','memory.social','memory.social_append','memory.remember'}


def reason_code(payload):
    if isinstance(payload.get('internal_review'),dict):return 'internal_review'
    from hakimi.core.context_window import select_context
    from hakimi.memory.memory_recall import opportunity
    from hakimi.memory.memory_lifecycle import social_signal
    target=select_context(payload)[1]
    cue=opportunity(target,str(payload.get('session_key','')))
    if cue:return cue.get('reason_code','explicit_history')
    if social_signal(target.get('text','')):return 'explicit_write_or_change'
    return 'agent_discretion'


def receipt_fields(name, output=None, error=None):
    fields={}
    if error is not None:
        fields.update(outcome='error',error_type=type(error).__name__)
    elif name in {'memory.social','memory.social_append','memory.remember'}:
        fields['outcome']='saved' if isinstance(output,dict) and output.get('saved') is True else 'unconfirmed'
    elif name=='memory.search':
        rows=output.get('memories',[]) if isinstance(output,dict) else []
        fields.update(outcome='results' if rows else 'empty',result_count=len(rows))
    elif name=='memory.read':fields['outcome']='returned'
    else:fields['outcome']='returned'
    return fields


def record(base,name,payload,output=None,error=None,provider="gemini"):
    if name not in TOOLS:return
    try:
        from pathlib import Path
        import json
        import time
        fields=receipt_fields(name,output,error)
        path=Path(base)/'state/memory_usage.jsonl'
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() and path.stat().st_size>1024*1024:
            with path.open('rb') as stream:
                stream.seek(-262144,2);tail=stream.read().partition(b'\n')[2]
            path.write_bytes(tail)
        value=dict(at=time.time(),tool=name,status='error' if error else 'ok',
                   trigger=reason_code(payload),provider=provider if provider in {'gemini','dsh','terra'} else 'other',**fields)
        with path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(value)+'\n')
    except Exception:
        pass  # Optional diagnostics must never affect the actual tool receipt.
