"""Collect the current speaker's text and permitted voice transcripts for this turn."""
import re

from hakimi.core.context_window import select_context

_NO_NETWORK = (r'(?:不要|别|不许|禁止|无需|不用|不必|不准|不需要|不得|不能|勿).{0,5}(?:联网|上网|搜|查|检索|访问网页)',
               r'(?:不联网|不上网)(?:回答|也|直接|，|,|。|$)',
               r'(?:只|仅)(?:用|根据|凭).{0,8}(?:已有知识|现有知识|本地资料|下面材料|我贴的|我给的|我提供的)',
               r'(?:仅|只|就)?(?:用|凭|根据)(?:你的|已有的|已有|现有)?记忆(?:回答|作答|就行|即可)?|离线(?:回答|作答|处理|模式)',
               r"\b(?:do not|don't|without|no)\s+(?:use\s+)?(?:web|internet|browsing|search)|\boffline only\b")


def network_allowed(payload,base=None):
    """False when the turn or the speaker's own words rule out web access for this turn."""
    if payload.get('allow_network') is False or payload.get('network_allowed') is False:
        return False
    text=current_text(payload,directives_only=True,base=base)
    return not any(re.search(pattern,text,re.I) for pattern in _NO_NETWORK)


def current_text(payload,*,directives_only=False,base=None,include_quotes=True):
    rows,target=select_context(payload)
    session=payload.get('session_key')
    all_rows=[row for row in payload.get('recent_context_messages',[]) if isinstance(row,dict)
              and row.get('session_key',session)==session and row.get('user_id')!='SELF']
    author=target.get('user_id')
    window=payload.get('listening_window') or {}
    ids=window.get('selected_message_ids',window.get('new_message_ids',[]))
    ids={str(value) for value in ids} if isinstance(ids,list) else set()
    pending=payload.get('pending_explicit_question_ids') or []
    pending={str(value) for value in pending} if isinstance(pending,list) else set()
    if ids:
        current=[row for row in all_rows if str(row.get('message_id','')) in ids|pending
                 and (row.get('user_id')==author if author else row is target)]
    else:
        anchor=next((i for i,row in enumerate(rows) if row is target),len(rows))
        current=[row for row in rows[anchor:] if row.get('user_id')!='SELF'
                 and row.get('user_id')==target.get('user_id')]
        current=[row for row in all_rows if row in current or
                 str(row.get('message_id','')) in pending and author and row.get('user_id')==author]
    seen={str(row.get('message_id')) for row in current}
    reference=str(target.get('reply_to') or '')
    for _ in range(3 if include_quotes else 0):
        quoted=next((row for row in all_rows if str(row.get('message_id'))==reference),None)
        if not quoted or reference in seen:break
        if not directives_only or author and quoted.get('user_id')==author:current.insert(0,quoted)
        seen.add(reference);reference=str(quoted.get('reply_to') or '')
    pieces=[]
    for row in current:
        text=str(row.get('text',''))[:12000]
        if 'voice.read message_id=' in text:
            from hakimi.memory.voice_receipts import policy_text
            from hakimi import paths
            transcript=policy_text(base or paths.base(),session,str(row.get('message_id','')))
            text+='\n'+(transcript or '禁止联网：此语音原文暂不可用')
        pieces.append(text)
    text='\n'.join(pieces)
    return text if directives_only else text[-8000:]
