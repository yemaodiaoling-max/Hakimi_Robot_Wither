"""Per-turn pacing hints from delivered-message counters (state lives in hakimi.chat.interaction_policy)."""
import math
from pathlib import Path
import time

from hakimi.core.runtime_io import read_json_cached
from hakimi.chat.interaction_policy import WINDOW, state


def guidance(payload,base,now=None):
    now=time.time() if now is None else now
    session=payload.get('session_key','')
    cfg=read_json_cached(Path(base)/'config.json',{})
    from hakimi.prompt.group_profiles import resolve_profile
    profile,_=resolve_profile(session,Path(base))
    count=sum(0<=now-at<WINDOW for at in state(base).get('groups',{}).get(session,[]))
    study=profile['name']=='study_calm' or session.removeprefix('group:') in {
        str(g) for g in cfg.get('participation_policy',{}).get('study_groups',[])}
    hints=[]
    if session.startswith('group:') and not study and count>6:
        scale=max(.45,math.exp(-.10*(count-6)))
        hints.append(f'本群8分钟内已发{count}条；本轮日常聊天长度偏好约为平时{scale:.0%}，自然缩短并给别人空间；不硬截断，必要的专业论证仍完整。')
    from hakimi.core.context_window import select_context
    target=select_context(payload)[1]
    rounds=state(base).get('users',{}).get(str(target.get('user_id','')),{}).get('rounds',0)
    if rounds and not session.startswith('private:'):hints.append(f'当前对象此前连续争吵{rounds}/3轮；仅确属对你争吵且希望结束拉扯时标conflict，正常分歧不标，AI互动标ai豁免。')
    from hakimi.chat.bot_registry import guidance as bot_guidance
    bot_note=bot_guidance(base,payload)
    if bot_note:hints.append(bot_note)
    return '\n'.join(hints)
