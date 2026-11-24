"""One local roll per image-led unsolicited opportunity."""
import math

def probability(affinity):
    score=float(affinity) if type(affinity) in (int,float) and math.isfinite(affinity) else 50.0
    score=max(0.0,min(100.0,score))
    return 0.25*(1.0+math.tanh((score-50.0)/30.0))

def image_candidates(session, mode, rows, pending_ids):
    if not session.startswith('group:') or '即时唤醒' in mode or mode=='冷场主动抛话题':
        return []
    return [r for r in rows if str(r.get('message_id','')) in pending_ids
            and r.get('user_id')!='SELF'
            and (r.get('image_paths') or '[群友发图' in str(r.get('text',''))
                 or '[表情包图片' in str(r.get('text','')) or '[CQ:image,' in str(r.get('text','')))]

def decide(entry, affinity, random_value):
    if 'image_roll' not in entry:
        p=probability(affinity)
        entry['image_roll']={'affinity':affinity,'probability':p,'roll':random_value,'accepted':random_value<p}
    return entry['image_roll']
