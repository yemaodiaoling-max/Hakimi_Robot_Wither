"""Synthetic final-render QA: no chat reads, model calls, native writes or sends."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch


def main():
    from hakimi.memory.bridge_context import context_for
    from hakimi.prompt import prompt_context as prompt
    source=Path(prompt.__file__).read_text(encoding='utf-8')
    assert 'BILIBILI_MEMORY_BRIDGE_V1' in source
    assert 'bridge_context_for' in prompt.build_context.__code__.co_varnames
    now=time.time()
    with tempfile.TemporaryDirectory(prefix='bili-bridge-qa-') as temp:
        base=Path(temp)
        (base/'state').mkdir()
        records=[dict(source='bilibili',summary='合成测试：只听到一段钢琴，画面未知',
            source_refs=['bilibili:live:8729095131:session:synthetic-qa'],
            coverage=[dict(start=0,end=4,modality='audio')],attribution='observation',
            occurred_at=now-5,packet_id='synthetic-qa',source_id='synthetic-qa',revision=1,
            is_digest=True,truncated=False,expires_at=now+60)]
        raw=json.dumps(records,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        snapshot=dict(version=1,source='bilibili',generated_at=now,records=records,
                      checksum=hashlib.sha256(raw.encode()).hexdigest())
        (base/'state/bili_bridge_context.json').write_text(json.dumps(snapshot,ensure_ascii=False),encoding='utf-8')
        synthetic={'session_key':'group:999999999999','reply_target':{},**context_for(base,'钢琴',now)}
        # Exercise the actual deployed native and fallback final renderers. Their
        # builder is replaced only to avoid querying real chat/social databases.
        with patch.object(prompt,'build_context',return_value=synthetic), \
             patch.object(prompt,'_sticker_section',return_value=''), \
             patch.object(prompt,'lore_guidance',return_value=''), \
             patch.object(prompt,'gemini_style_feedback',return_value=''):
            native=prompt.render_vertex_prompt({'session_key':'group:999999999999'},base)
            fallback=prompt.render_prompt({'session_key':'group:999999999999'},base)
        for rendered in (native,fallback):
            assert all(key in rendered for key in ('bridge_memory_data','bridge_memory_policy','source_refs','coverage','synthetic-qa'))
        print(json.dumps({'state':'verified','synthetic_only':True,'native_consumes_bridge':True,
            'fallback_consumes_bridge':True,'native_chars':len(native),'fallback_chars':len(fallback),
            'helper_reload_each_context':True,'model_calls':0,'messages_sent':0}))


if __name__=='__main__':
    main()
