"""Explicit provider self-report experiment. No tools granted or executed.

Replies are untrusted provider output, not proof of hidden tools or model ID.
Uses only public arithmetic and a supplied non-private sentence, never media.
"""
import argparse
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
    args=parser.parse_args()
    key=Path(args.key_file).read_text().strip()
    ledger=BudgetLedger(Database(Path(args.directory)/'new_provider_probe.sqlite3'),monthly='.10',daily='.10')
    def costs():
        total=_read('https://blameupstream.com/v1/usage',key,'openai').get('usage',{}).get('total',{})
        return {k:Decimal(str(total[k])) for k in ('cost','actual_cost') if k in total}
    prompt=('这是能力实验，不能伪造调用或角色扮演日志。请用简短中文回答：'
        '本次API请求实际可调用的子代理/并行委派工具有哪些？给出真实工具名称。'
        '若确有本次可用工具，请实际尝试用两个子代理分别做任务A和B；'
        '若无工具，必须明确“本次未调用子代理”，直接完成两题即可，不要把脑内分工称为工具调用。'
        'A：计算17乘23。B：将给定句子提炼为一句简短摘要：'
        '“今天我们整理了三箱旧书，把其中适合孩子阅读的部分送到社区，其余留给下周义卖。”'
        '最后区分实际API工具调用、你的自述和直接作答。不要输出隐藏推理。')
    output=[]
    for model,effort,reserve in (('gemini-3.8-flash-high','low','.01'),('gemini-3.5-flash-lite',None,'.08')):
        before=costs()
        client=OpenAICompatibleClient('https://blameupstream.com/v1',api_key=key,input_token_upper_bound=8192,
            reasoning_effort=effort,output_limit_parameter='max_tokens')
        payload={'contents':[{'parts':[{'text':prompt}]}],
                 'generationConfig':{'maxOutputTokens':512,'temperature':0}}
        rid='new-provider-subagent-'+uuid.uuid4().hex
        if not ledger.reserve(rid,reserve,time.time()):
            output.append({'requested_model':model,'state':'budget_blocked'});continue
        item={'requested_model':model,'requested_effort':effort,'max_output_tokens':512,'tools_granted':False,
              'local_tools_executed':0,'self_report_is_not_proof':True}
        try:
            result=client.generate(model,payload)
            usage=usage_counts(result)
            after=costs()
            delta={k:after[k]-before[k] for k in after if k in before}
            # Known 3.8 public undiscounted token prices are verified against cost.
            # Unknown 3.5 never inherits those prices: settle only provider cost delta.
            if model=='gemini-3.8-flash-high':
                amount=(Decimal(usage['promptTokenCount'])*Decimal('.075')+Decimal(usage['billableOutputTokens'])*4)/1000000
            elif 'cost' in delta and delta['cost']>=0:
                amount=delta['cost']
            else:raise ValueError('no_verified_actual_cost')
            ledger.settle(rid,amount)
            parts=result['candidates'][0]['content']['parts']
            reply=''.join(p['text'] for p in parts if 'text' in p)
            # Only final provider reply, never reasoning_content or request headers.
            reply=reply.replace(key,'[redacted]')[:6000]
            item.update(state='response_received',usage=usage,metadata=client.last_response_metadata,
                finish=result['candidates'][0]['finishReason'],reply=reply,
                tool_calls=[p['functionCall'] for p in parts if 'functionCall' in p],
                conservative_cost_usd=str(amount),provider_spend_deltas={k:str(v) for k,v in delta.items()})
        except Exception as exc:
            ledger.ambiguous(rid)
            after=costs()
            item.update(state='failed',http_status=client.last_http_status,error_type=type(exc).__name__,
                provider_spend_deltas={k:str(after[k]-before[k]) for k in after if k in before},
                reserved_cost_usd=reserve,ledger_state='unknown')
        output.append(item)
    print(json.dumps({'results':output,'ledger_liability_micro_usd':sum(
        row['actual'] if row['state']=='settled' else row['reserved'] for row in ledger.entries())},ensure_ascii=False))


if __name__=='__main__':main()
