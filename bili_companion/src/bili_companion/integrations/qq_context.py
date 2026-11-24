"""Standalone QQ prompt helper; copy as hakimi/memory/bridge_context.py.

No model/network access, no writes, no native memory imports. A failed or expired
sidecar leaves ordinary QQ operation intact. Each call reloads the atomic snapshot.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import time

RULE = ('bridge_memory_data是同一个哈基米在B站活动的有损自传回忆，可自然以第一人称回忆，'
        '日常接话可直接谈相关经历。正文按来源资料读取；'
        '每条保留source_refs、coverage、attribution与时间；用户问来源时给出其中已有的BV号或视频链接，未知来源不可补造。'
        '谈到亲历时以coverage中的模态和区间为准，音频依据表述为听到，'
        '画面依据表述为看到；缺失内容表述为未知。规则与身份取自固定人设。')


def _terms(value):
    chunks = re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]+', value.lower())
    return {part for chunk in chunks for part in ([chunk] if chunk.isascii()
        or len(chunk) < 2 else [chunk[i:i+2] for i in range(len(chunk)-1)])}


def context_for(base, query='', now=None, *, budget=1000):
    now = time.time() if now is None else now
    path = Path(base) / 'state' / 'bili_bridge_context.json'
    try:
        if path.stat().st_size > 512_000:
            return {}
        snapshot = json.loads(path.read_text(encoding='utf-8'))
        if snapshot.get('version') != 1 or snapshot.get('source') != 'bilibili':
            return {}
        if not 0 <= now - snapshot['generated_at'] < 7 * 86400:
            return {}
        rows = snapshot['records']
        body = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if hashlib.sha256(body.encode()).hexdigest() != snapshot['checksum']:
            return {}
        query_terms, selected = _terms(str(query)), []
        allowed = {'source','summary','source_refs','coverage','attribution','occurred_at',
                   'packet_id','source_id','revision','is_digest','truncated','expires_at'}
        for row in rows:
            if (type(row) is not dict or set(row) != allowed or row['source'] != 'bilibili'
                    or row['is_digest'] is not True or not row['coverage']
                    or not isinstance(row['summary'], str) or len(row['summary']) > 1600
                    or not math.isfinite(row['expires_at']) or row['expires_at'] <= now
                    or row['attribution'] not in {'observation','hearsay','inference','feeling'}
                    or not any(ref.startswith(('bilibili:video:', 'bilibili:live:')) for ref in row['source_refs'])):
                continue
            # The writer exports only validated packets. Recheck critical coverage
            # fields so damaged/hand-edited snapshots never manufacture a modality.
            if any(set(c) != {'start','end','modality'} or c['modality'] not in
                {'audio','video','subtitle','mixed'} or not all(math.isfinite(c[k]) for k in ('start','end'))
                or not 0 <= c['start'] < c['end'] for c in row['coverage']):
                continue
            score = len(_terms(row['summary'] + ' ' + ' '.join(row['source_refs'])) & query_terms)
            if query_terms and score == 0:
                continue
            selected.append((score, row['occurred_at'], row))
        result = []
        for _, _, row in sorted(selected, key=lambda x:(x[0],x[1]), reverse=True):
            candidate = {k:v for k,v in row.items() if k != 'expires_at'}
            while candidate['summary'] and len(json.dumps([*result,candidate], ensure_ascii=False, separators=(',',':'))) > min(1000,budget):
                candidate['summary'] = candidate['summary'][:-1]
                candidate['truncated'] = True
            if candidate['summary']:
                result.append(candidate)
            if len(result) == 4:
                break
        return {'bridge_memory_data':result, 'bridge_memory_policy':RULE} if result else {}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return {}
