import argparse
from datetime import date
from decimal import Decimal
import json
import sys
from pathlib import Path
from bili_companion.domain import Evidence, Scope, Candidate
from bili_companion.memory import MemoryStore
from bili_companion.orchestrator import Scheduler
from bili_companion.policy.budget import BudgetLedger
from bili_companion.policy.danmaku import ActionExecutor
from bili_companion.runtime.config import load
from bili_companion.runtime.storage import Database


def sync_demo(args):
    """Local synthetic QQ digest -> Bili demo; never manufactures a watch history."""
    from bili_companion.sync import SyncStore, SyncWorker, SourceBatch, SourceItem
    db = Database(args.db)
    qq = SyncStore(db, endpoint='qq', local_id='synthetic-qq', peer_id='synthetic-bili')
    bili = SyncStore(db, endpoint='bilibili', local_id='synthetic-bili', peer_id='synthetic-qq')
    fixture = json.loads(Path(args.fixture).read_text(encoding='utf-8'))
    items = tuple(SourceItem(**{**item, 'source_refs':tuple(item.get('source_refs', ())),
                                'coverage':tuple(item.get('coverage', ()))}) for item in fixture['items'])
    class LocalTransport:
        def send(self, packet):
            bili.receive(packet)
            return True
    worker = SyncWorker(qq, lambda _:SourceBatch(items, fixture['checkpoint']), LocalTransport())
    result = worker.run_once(force=args.force)
    print(json.dumps({'synthetic_only':True, 'result':result,
                      'bridge_records':len(bili.snapshot_records()),
                      'bridge_context_characters':len(json.dumps(bili.recall(), ensure_ascii=False)),
                      'bilibili_native_watch_records':0}, ensure_ascii=False))


def replay(args):
    config = load(args.config)
    db = Database(args.db)
    scheduler = Scheduler(db, config['priority_room'])
    memory = MemoryStore(db)
    ledger = BudgetLedger(db, config['monthly_usd'], config['daily_usd'])
    executor = ActionExecutor(db, memory, config)
    for line in Path(args.events).read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        kind = event['type']
        if kind == 'evidence':
            payload = event['evidence']
            payload['scope'] = Scope(**payload['scope'])
            result = memory.append(Evidence(**payload))
        elif kind == 'candidate':
            payload = event['candidate']
            payload['evidence_ids'] = tuple(payload['evidence_ids'])
            result = executor.preview(Candidate(**payload), scheduler.snapshot(),
                account_id=config['account_id'], watch_epoch=event['watch_epoch'], now=event['at'])
        elif kind == 'reserve':
            result = ledger.reserve(event['id'], event['upper_usd'], event['at'])
        elif kind == 'settle':
            result = ledger.settle(event['request_id'], event['actual_usd'])
        elif kind == 'ambiguous':
            result = ledger.ambiguous(event['request_id'])
        else:
            result = scheduler.apply(event)
        print(json.dumps({'type': kind, 'result': result}, ensure_ascii=False))


def estimate(args):
    config = load(args.config)
    if args.hours < 0 or args.observation_output_tokens < 0 or args.frames_per_second < 0:
        raise ValueError('estimation values cannot be negative')
    observer, decision = config['models']['observer'], config['models']['decision']
    when = date.fromisoformat(args.date)
    price_assumptions = []
    def rates(model):
        if model.get('future_from') and when >= date.fromisoformat(model['future_from']):
            return Decimal(model['future_input_per_million']), Decimal(model['future_output_per_million'])
        if when > date.fromisoformat(model['price_until']):
            if not args.assume_current_prices:
                raise ValueError('price review expired; update config or explicitly use --assume-current-prices')
            price_assumptions.append(f"假设 {model['id']} 沿用已过复核期限的当前价；非官方未来价格保证")
        return Decimal(model['input_per_million']), Decimal(model['output_per_million'])
    oi, oo = rates(observer)
    di, do = rates(decision)
    visual = Decimal('66') * Decimal(str(args.frames_per_second)) * 3600
    audio = Decimal(32 * 3600)
    hourly = ((visual + audio + 120*600)*oi + Decimal(120*args.observation_output_tokens)*oo
              + Decimal(12*1500)*di + Decimal(12*80)*do)/1_000_000
    base = hourly * Decimal(str(args.hours))
    total = (base + Decimal('.30')) * Decimal('1.20')
    print(json.dumps({'hours': args.hours, 'hourly_usd': str(hourly), 'base_usd': str(base),
        'with_memory_and_20_percent_margin_usd': str(total),
        'fps': args.frames_per_second,
        'price_assumptions': price_assumptions,
        'assumptions': '容量估算非账单；假设static+low、66 token/frame（需接入验证），连续音频32 token/秒；观察输出含思考；无突发、无媒体复核、无重试；不是预算保证'}, ensure_ascii=False))


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='离线 B站观看机器人基础')
    parser.add_argument('--config', default='configs/example.toml')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('replay', help='仅回放合成事件，不访问网络')
    run.add_argument('events')
    run.add_argument('--db', default='state/replay.sqlite3')
    cost = sub.add_parser('estimate-cost', help='容量敏感性估算，不调用模型')
    cost.add_argument('--hours', type=float, default=60)
    cost.add_argument('--date', default='2026-11-01')
    cost.add_argument('--frames-per-second', type=float, default=1.0)
    cost.add_argument('--observation-output-tokens', type=int, default=80)
    cost.add_argument('--assume-current-prices', action='store_true',
                      help='显式假设未公布未来价格的模型沿用当前价；已公布未来价仍优先')
    demo = sub.add_parser('sync-demo', help='本地合成记忆摘要同步；无网络、无观看经历')
    demo.add_argument('--fixture', default='examples/sync/qq_public_synthetic.json')
    demo.add_argument('--db', default='state/sync-demo.sqlite3')
    demo.add_argument('--force', action='store_true', help='忽略定时到期；仍保留幂等和租约检查')
    args = parser.parse_args()
    try:
        if args.command == 'replay':
            replay(args)
        elif args.command == 'sync-demo':
            sync_demo(args)
        else:
            estimate(args)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f'错误：{exc}\n')


if __name__ == '__main__':
    main()
