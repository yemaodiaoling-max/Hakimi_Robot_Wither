"""Presentation-only focus and exact deduplication; never changes store authority."""
import re
import json


def focus_rows(messages, target):
    rows=list(messages or [])
    active={str(row.get('user_id','')) for row in rows[-6:]}
    active.add(str(target.get('user_id','')))
    text=str(target.get('text',''))
    active.update(re.findall(r'(?:@|qq=)([1-9][0-9]{0,19})',text))
    # Reuse only exact, unique names already mapped in the current transcript.
    names={}
    for row in rows:
        name=str(row.get('nickname',''))
        if len(name)>=2:names.setdefault(name,set()).add(str(row.get('user_id','')))
    for name,ids in names.items():
        if len(ids)==1 and name in text:active.update(ids)
    references={str(target.get('reply_to','')),str(target.get('reply_to_message_id',''))}
    references.update(re.findall(r'(?:\[引用:|\[CQ:reply,id=)([^\],]+)',text))
    for row in rows:
        if str(row.get('message_id','')) in references and row.get('message_id'):
            active.add(str(row.get('user_id','')))
    focused=[row for row in rows if str(row.get('user_id','')) in active]
    present={str(row.get('user_id','')) for row in focused}
    # These synthetic identity rows are retrieval inputs only, never chat evidence.
    focused.extend({'user_id':user,'text':''} for user in active-present
                   if re.fullmatch(r'[1-9][0-9]{0,19}',user))
    return focused


def unique_messages(messages):
    seen=set();result=[]
    for row in messages:
        mid=str(row.get('message_id',''))
        signature=json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':'))
        if mid and signature in seen:continue
        if mid:seen.add(signature)
        result.append(row)
    return result


def bounded_memories(notes, budget=600, limit=4):
    result=[];seen=set()
    for note in notes:
        note=str(note)
        # Only exact repeated representations are removed. Same M with different
        # excerpts/source metadata remains distinct, avoiding loss of evidence.
        if note in seen:continue
        seen.add(note)
        if len(note)>budget:continue
        result.append(note);budget-=len(note)
        if len(result)>=limit:break
    return result
