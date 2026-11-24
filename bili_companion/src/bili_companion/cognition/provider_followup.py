"""Two authorized follow-up probes with reconciled, independent test budget."""
import argparse
import base64
from decimal import Decimal
import json
from pathlib import Path
import time
import uuid
from bili_companion.policy.budget import BudgetLedger
from bili_companion.runtime.storage import Database
from .openai_provider import OpenAICompatibleClient
from .provider_probe import _read
from .live import usage_counts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True)
    parser.add_argument('--key-file',required=True)
    args=parser.parse_args();root=Path(args.directory);key=Path(args.key_file).read_text().strip()
    db=Database(root/'new_provider_probe.sqlite3');ledger=BudgetLedger(db,monthly='.10',daily='.10')
    # The operator verified the single lite POST was HTTP404, with serial cost
    # AND actual_cost deltas both zero. Persist that reconciliation, not a guess.
    with db.connect(write=True) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS probe_reconciliations(id TEXT PRIMARY KEY,at REAL,reason TEXT,source TEXT)')
    for row in ledger.entries():
        if row['state']=='unknown' and row['id'].startswith('new-provider-subagent-') and row['reserved']==80000:
            ledger.settle(row['id'],'0')
            with db.connect(write=True) as connection:
                connection.execute('INSERT OR IGNORE INTO probe_reconciliations VALUES (?,?,?,?)',(
                    row['id'],time.time(),'HTTP404; serial usage.total.cost delta=0; actual_cost delta=0; operator confirmed',
                    'https://blameupstream.com/v1/usage'))
    def cost():
        usage=_read('https://blameupstream.com/v1/usage',key,'openai').get('usage',{}).get('total',{})
        return {k:Decimal(str(usage[k])) for k in ('cost','actual_cost') if k in usage}
    results=[]
    try:
        listing=_read('https://blameupstream.com/v1beta/models',key,'gemini')
        results.append({'probe':'native_gemini_list','state':'reachable','field_names':sorted(listing)[:12]})
    except Exception as exc:
        results.append({'probe':'native_gemini_list','state':'unavailable','http_status':getattr(exc,'http_status',None)})
    for label,maximum,reserve in (('short_subagent',1024,'.006'),('audio_video',1536,'.022')):
        client=OpenAICompatibleClient('https://blameupstream.com/v1',api_key=key,input_token_upper_bound=200000,
            reasoning_effort='low',output_limit_parameter='max_tokens')
        if label=='short_subagent':
            parts=[{'text':'能实际调用子代理吗？只答能/不能；不能就停止，不模拟。'}]
        else:
            media=base64.b64encode((root/'sample.mp4').read_bytes()).decode()
            parts=[{'inlineData':{'mimeType':'video/mp4','data':media},'videoMetadata':{'fps':.2}},
                {'text':'这是实际8秒音画技术测试。只返回严格JSON {"audio_event":"实际听到什么，最多60字，不把画面字当声音",'
                '"video_event":"实际看见什么，最多60字"}。不能听到或看清就明确说明，不编造。不要markdown或额外说明。'}]
        payload={'contents':[{'parts':parts}], 'generationConfig':{'maxOutputTokens':maximum,'temperature':0}}
        if label=='audio_video':payload['generationConfig']['responseMimeType']='application/json'
        client.count_tokens('gemini-3.8-flash-high',payload)
        before=cost();rid='new-provider-followup-'+label+'-'+uuid.uuid4().hex
        if not ledger.reserve(rid,reserve,time.time()):
            results.append({'probe':label,'state':'budget_blocked'});continue
        output={'probe':label,'max_tokens':maximum,'requested_effort':'low','tools_granted':False,'messages_sent':0}
        try:
            response=client.generate('gemini-3.8-flash-high',payload)
            usage=usage_counts(response)
            amount=(Decimal(usage['promptTokenCount'])*Decimal('.075')+Decimal(usage['billableOutputTokens'])*4)/1000000
            ledger.settle(rid,amount);after=cost()
            text=''.join(p.get('text','') for p in response['candidates'][0]['content']['parts']).replace(key,'[redacted]')
            output.update(state='response_received',usage=usage,metadata=client.last_response_metadata,
                finish=response['candidates'][0]['finishReason'],reply=text[:10000],
                tool_calls=[p['functionCall'] for p in response['candidates'][0]['content']['parts'] if 'functionCall' in p],
                conservative_cost_usd=str(amount),provider_spend_deltas={k:str(after[k]-before[k]) for k in after if k in before})
            if label=='audio_video':
                try:
                    parsed=json.loads(text)
                    output['strict_json']=type(parsed) is dict and set(parsed)=={'audio_event','video_event'}
                except Exception:output['strict_json']=False
            (root/(label+'_safe_response.json')).write_text(json.dumps(output,ensure_ascii=False),encoding='utf-8')
        except Exception as exc:
            ledger.ambiguous(rid)
            output.update(state='failed',error_type=type(exc).__name__,http_status=client.last_http_status,
                          error_features=client.last_error_features)
        results.append(output)
    print(json.dumps({'results':results,'ledger_liability_micro_usd':sum(
        r['actual'] if r['state']=='settled' else r['reserved'] for r in ledger.entries())},ensure_ascii=False))


if __name__=='__main__':main()
