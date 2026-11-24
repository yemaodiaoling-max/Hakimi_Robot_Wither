"""Final isolated engine/persona/role-dispatch smoke; never dispatches effects."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid
from bili_companion.domain import Scope
from bili_companion.memory import MemoryStore
from bili_companion.persona import PersonaLoader
from bili_companion.policy.budget import BudgetLedger
from bili_companion.runtime.storage import Database
from .live import CognitionEngine
from .openai_provider import OpenAICompatibleClient


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True);parser.add_argument('--key-file',required=True)
    parser.add_argument('--persona-dir',required=True)
    args=parser.parse_args();root=Path(args.directory)
    metadata=json.loads((root/'metadata.json').read_text())
    path=root/'sample.mp4'
    probe=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(path)],capture_output=True,check=True)
    duration=float(json.loads(probe.stdout)['format']['duration'])
    scope=Scope('new-provider-isolated-smoke','video',metadata['bvid'])
    chunk=SimpleNamespace(path=path,start=0.,end=duration,received_at=path.stat().st_mtime,
        modality='mixed',target_id=scope.identity,session_id='provider-engine-smoke-'+uuid.uuid4().hex)
    db=Database(root/'new_provider_probe.sqlite3');ledger=BudgetLedger(db,monthly='.10',daily='.10')
    client=OpenAICompatibleClient('https://blameupstream.com/v1',api_key_file=args.key_file,input_token_upper_bound=150000,
        media_mode='mp4_image_url',reasoning_effort='low',output_limit_parameter='max_tokens')
    engine=CognitionEngine(client,ledger,MemoryStore(db),persona_loader=PersonaLoader(args.persona_dir),
        observer_model='gemini-3.8-flash-high',decider_model='gemini-3.8-flash-high',
        price_resolver=lambda model,now:(Decimal('.075'),Decimal('4')),max_output_tokens=1536,
        request_usd_limit='.03',child_roles_enabled=True,epoch_is_current=lambda s,session,epoch:epoch==0)
    result=engine.observe(chunk,scope,0,allow_decision=True)
    output={'state':result.status,'interest':result.interest,'next_action':result.next_action,'summary':result.summary,
        'comment_status':result.comment_status,'child_status':getattr(result,'child_status',None),
        'request_ids':result.request_ids,'usage':result.usage,'metadata':client.last_response_metadata,
        'input_envelope':client.last_input_envelope,
        'events':[dict(text=e.text,start=e.media_start,end=e.media_end,modality=e.modality,confidence=e.confidence) for e in result.observation.evidence],
        'messages_sent':0,'native_memory_written':False,
        'ledger_liability_micro_usd':sum(r['actual'] if r['state']=='settled' else r['reserved'] for r in ledger.entries())}
    with db.connect() as connection:
        output['role_calls']=[dict(row) for row in connection.execute('SELECT role,model,purpose FROM cognition_role_calls WHERE request_id IN ('+
            ','.join('?' for _ in result.request_ids)+')',result.request_ids)] if result.request_ids else []
    (root/'engine_safe_response.json').write_text(json.dumps(output,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(output,ensure_ascii=False))


if __name__=='__main__':main()
