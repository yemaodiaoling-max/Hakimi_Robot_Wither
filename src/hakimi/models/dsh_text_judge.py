"""DSH headless text selection, using the normal installation and project context."""
import json
from pathlib import Path
import re
import time
from functools import lru_cache

from hakimi.models.fallback_runner import discover_installation, _provider, ALL_ROWS, ENABLED_ROWS


class JudgeError(RuntimeError):
    pass


TRANSPORT_TRACE = r"""
const emit=(phase,extra={})=>process.stderr.write('\nHAKIMI_TRACE '+JSON.stringify({phase,at:Date.now(),...extra})+'\n');
emit('node_started');
const original=globalThis.fetch;
globalThis.fetch=async function(input,options){
 if(!String(input).includes('/chat/completions')) return original(input,options);
 emit('api_request');
 const response=await original(input,options);emit('api_headers',{status:response.status});
 if(!response.body||!response.ok)return response;
 let buffer='',reasoning=false,text=false;const decoder=new TextDecoder();
 const stream=response.body.pipeThrough(new TransformStream({transform(chunk,controller){
  buffer+=decoder.decode(chunk,{stream:true});const lines=buffer.split('\n');buffer=lines.pop();
  for(const line of lines){if(!line.startsWith('data:'))continue;try{
   const event=JSON.parse(line.slice(5));if(event.usage){globalThis.__HAKIMI_TEXT_USAGE=event.usage;emit('api_usage',{usage:event.usage});}for(const choice of event.choices||[]){
    if(choice.delta?.reasoning_content&&!reasoning){reasoning=true;emit('first_reasoning');}
    if(choice.delta?.content&&!text){text=true;emit('first_text');}
    if(choice.finish_reason)emit('api_finish',{reason:choice.finish_reason});
   }
  }catch{}}
  controller.enqueue(chunk);
 },flush(){emit('stream_end');}}));
 return new Response(stream,{status:response.status,statusText:response.statusText,headers:response.headers});
};
process.on('exit',()=>emit('node_exit'));
"""

def model_patch(provider):
    """Configuration only: no additional persona or behavioral instructions."""
    # Same installation, home and cwd; skip coding-only plugins for this text task.
    rows = [{"id": name, "disabled": True} for name in ALL_ROWS if name not in ENABLED_ROWS]
    return rows + [
        {"id": "system-prompt", "config": {"includeHarnessIdentity": False,
            "includeRuntimeContext": False, "personaPrefix": "", "personaSuffix": ""}},
        {"id": "agent-default-model", "config": {
            "provider": "deepseek-official", "model": "deepseek-flash", "reasoningEffort": "low"}},
        {"id": "llm-deepseek", "config": {
            "apiKeyEnv": "DEEPSEEK_API_KEY", "baseURL": provider["base_url"],
            "thinking": "enabled", "reasoningEffort": "low", "maxTokens": 8192}},
    ]


def parse_selection(raw):
    text = str(raw).strip().lstrip("\ufeff")
    fence = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        result = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise JudgeError("TEXT_JUDGE_INVALID_JSON") from exc
    ids = result.get("selected_message_ids") if isinstance(result, dict) else None
    if not isinstance(ids, list) or any(type(value) not in (str, int) for value in ids):
        raise JudgeError("TEXT_JUDGE_INVALID_SELECTION")
    # Model usage is intentionally not returned or added to Gemini accounting.
    return {"selected_message_ids": list(dict.fromkeys(str(value) for value in ids))}


@lru_cache(maxsize=1)
def _installation():
    return discover_installation()


def _phase_times(stderr, started_at=None):
    events = {}
    for line in stderr.decode('utf-8', errors='replace').splitlines():
        if not line.startswith('HAKIMI_TRACE '):continue
        try:
            event = json.loads(line[len('HAKIMI_TRACE '):])
            events.setdefault(event['phase'], event)
        except (ValueError, KeyError, TypeError):continue
    result = {}
    if started_at is not None and 'api_request' in events:
        result['startup_ms'] = round(events['api_request']['at']-started_at)
    for label, first, last in (
            ('node_setup_ms', 'node_started', 'api_request'),
            ('api_headers_ms', 'api_request', 'api_headers'),
            ('first_reasoning_ms', 'api_request', 'first_reasoning'),
            ('first_text_ms', 'api_request', 'first_text'),
            ('generation_ms', 'api_headers', 'api_finish'),
            ('shutdown_ms', 'api_finish', 'node_exit')):
        if first in events and last in events:
            result[label] = events[last]['at']-events[first]['at']
    if 'api_headers' in events:result['http_status'] = events['api_headers']['status']
    if 'api_finish' in events:result['finish_reason'] = events['api_finish']['reason']
    result['last_phase'] = next(reversed(events), 'before_node')
    return result


def _timing(base, started, launched, task, result, *, phases=None, parse_ms=None):
    # Timing/size only: no chat text, credentials, or DSH token accounting.
    path = base / "state" / "dsh_text_judge_timing.jsonl"
    try:
        if path.exists() and path.stat().st_size > 131072:
            kept = path.read_bytes().splitlines()[-100:]
            path.write_bytes(b"\n".join(kept)+b"\n")
        row = {"at": time.time(), "setup_ms": round((launched-started)*1000),
               "elapsed_ms": round((time.monotonic()-started)*1000),
               "task_chars": len(task), "result": result, **(phases or {})}
        if parse_ms is not None:row['parse_ms'] = parse_ms
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row)+'\n')
    except OSError:
        pass


def run_judge(task, base, timeout_seconds=30, *, thinking="low"):
    from hakimi.models.dsh_text_worker import worker
    return worker.run(task, base, timeout_seconds, thinking)
