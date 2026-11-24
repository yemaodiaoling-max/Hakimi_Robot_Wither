"""Save a memory note proposed in a DSH fallback reply."""
def save_proposal(result,claim,coordinator,base):
    """The compact reply+memory form also writes before any 'remembered' reply is queued."""
    if not result.get('memory'):return None
    if not coordinator.check_token(claim['token'],'dsh'):raise ValueError('本轮已过期')
    from hakimi.voice.store import has_voice,scoped_saved_receipt
    if has_voice(base,claim['payload']):return scoped_saved_receipt()
    from hakimi.memory.smart_memory import remember_memory,visible_users
    from hakimi.core.context_window import select_context
    from hakimi.tools.math_render import log_tool_call
    payload=claim['payload']
    receipt=remember_memory(base,payload['session_key'],result['memory'],visible_users(payload),provider='dsh',
        origin_message_id=str(select_context(payload)[1].get('message_id','')),channel='dsh_reply')
    log_tool_call(base,'memory.remember','ok' if receipt.get('saved') else 'error',provider='dsh')
    if not receipt.get('saved'):raise ValueError('记忆未保存：'+str(receipt.get('error','unknown')))
    return receipt
