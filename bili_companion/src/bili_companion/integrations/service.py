"""One cheap, local, bidirectional bridge tick. No platform or model clients."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from bili_companion.runtime.storage import Database
from bili_companion.runtime.retention import prune_activity
from bili_companion.sync import SourceBatch, SyncStore, SyncWorker
from bili_companion.sync.types import canonical
from bili_companion.memory.sources import linked_refs
from .qq import BilibiliSource
from .qq_interest import QQInterestSampler


class LocalTransport:
    """Same-host account-bound transport; receive commits before acknowledgement."""
    def __init__(self, peer, source_store=None):
        self.peer = peer
        self.source_store = source_store
        self.imported = []

    def send(self, packet):
        self.peer.receive(packet)
        if packet['origin'] == 'qq' and packet['kind'] == 'qq_digest':
            with self.peer.database.connect(write=True) as db:
                db.execute("CREATE TABLE IF NOT EXISTS qq_interest_imports (channel TEXT, source_id TEXT, revision INTEGER, read_at REAL, keyword TEXT DEFAULT '', PRIMARY KEY(channel,source_id,revision))")
                if 'keyword' not in {r[1] for r in db.execute('PRAGMA table_info(qq_interest_imports)')}:
                    db.execute("ALTER TABLE qq_interest_imports ADD COLUMN keyword TEXT DEFAULT ''")
                for fact in packet['facts']:
                    keyword = ''
                    if self.source_store:
                        with self.source_store.database.connect() as source_db:
                            row = source_db.execute('SELECT keyword FROM qq_interest_seen WHERE channel=? AND source_id=?',
                                (self.source_store.channel, fact['source_id'])).fetchone()
                            keyword = row[0] if row else ''
                    cursor = db.execute('INSERT OR IGNORE INTO qq_interest_imports VALUES (?,?,?,?,?)',
                        (self.peer.channel, fact['source_id'], fact['revision'], self.peer.clock(), keyword))
                    if cursor.rowcount:
                        self.imported.append(dict(source_id=fact['source_id'], revision=fact['revision'], summary=fact['text'][:200], keyword=keyword, read_at=self.peer.clock()))
        return True  # duplicate receipt is also a durable acknowledgement


def write_snapshot(store, path):
    """QQ can relevance-select older episodes without adding a package dependency."""
    now = store.clock()
    records = store.snapshot_records(limit=1000)
    # Derived retrieval labels; never alter immutable source facts or revisions.
    # Keep the existing snapshot schema so the running QQ helper sees them now.
    tagged = []
    for record in records:
        refs = record['source_refs']
        kinds = []
        if any(ref.startswith('bilibili:live:') for ref in refs):
            kinds.append('直播')
        if any(ref.startswith('bilibili:video:') for ref in refs):
            kinds.append('视频')
        prefix = '[B站' + ''.join('·' + kind for kind in kinds) + '] '
        enriched = linked_refs(refs)
        # Put known BV before the lossy text so the existing QQ helper can find
        # it immediately and preserve it when trimming a long recollection.
        bvids = [ref.rsplit('/', 1)[-1] for ref in enriched
                 if ref.startswith('https://www.bilibili.com/video/')]
        locator = ('来源：' + '、'.join(bvids) + '。') if bvids else ''
        tagged.append({**record, 'summary': prefix + locator + record['summary'],
                       'source_refs': enriched})
    records = tagged
    while len(canonical(records).encode()) > 480_000:
        records.pop()
    snapshot = dict(version=1, source='bilibili', generated_at=now, records=records,
        checksum=hashlib.sha256(canonical(records).encode()).hexdigest())
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.bridge-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(snapshot, handle, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return len(records)


def _host_name(room_id):
    """The configured regular room's host name; any other room stays unnamed."""
    from bili_companion.persona import PersonaLoader
    return PersonaLoader().live_host(room_id).get('name', '')


def tick(config, *, force=False, clock=time.time, rng=None):
    state = Path(config['state_dir'])
    qq = SyncStore(Database(state/'qq_bridge.sqlite3'), endpoint='qq',
        local_id=config['qq_identity'], peer_id=config['bilibili_identity'], clock=clock)
    bili = SyncStore(Database(state/'bilibili_bridge.sqlite3'), endpoint='bilibili',
        local_id=config['bilibili_identity'], peer_id=config['qq_identity'], clock=clock)
    prune_activity(bili.database,clock(),scope='bridge')
    qq_source = QQInterestSampler(config['qq_database'], qq, rng=rng,
        keywords=config.get('qq_interest_keywords', ('音乐', '科学', '游戏', '动物', '生活', '艺术', '历史', '偏好')),
        owner_private_session=config.get('owner_private_session', ''))
    # Migrate existing six-hour schedules without waking on each timer tick.
    with qq.database.connect(write=True) as db:
        db.execute('UPDATE sync_state SET next_due=MIN(next_due,last_sync+7200) WHERE channel=? AND last_sync IS NOT NULL', (qq.channel,))
    native = config.get('bilibili_database')
    if native:
        bili_source = BilibiliSource(native, bili, account_id=config['bilibili_account_id'])
    else:
        # A disabled collector has no observations; never seed an invented episode.
        bili_source = lambda checkpoint: SourceBatch((), checkpoint)
    transport = LocalTransport(bili, qq)
    outbound_qq = SyncWorker(qq, qq_source, transport, interval_seconds=7200).run_once(force=force)
    if hasattr(qq_source, 'last_state'):
        outbound_qq.update(sampling_state=qq_source.last_state)
    if hasattr(qq_source, 'last_state') or transport.imported:
        # One durable row per actual sampling pass, including empty results.
        with bili.database.connect(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS qq_memory_reads (id INTEGER PRIMARY KEY, read_at REAL, keyword TEXT, status TEXT, records_json TEXT)')
            prior = db.execute('SELECT COUNT(*) FROM qq_interest_imports').fetchone()[0] > len(transport.imported) if transport.imported else False
            # A retry can flush several packets. Preserve every received record,
            # splitting into bounded groups with the actual stored retrieval term.
            groups = []
            for item in transport.imported:
                if (not groups or groups[-1][0] != item['keyword'] or len(groups[-1][1]) >= 6
                        or sum(len(x['summary']) for x in groups[-1][1]) + len(item['summary']) > 1000):
                    groups.append((item['keyword'], []))
                groups[-1][1].append(item)
            if not groups:
                groups = [(getattr(qq_source, 'last_keyword', ''), [])]
            for keyword, items in groups:
                status = ('imported' if prior else 'first_import') if items else ('delivery_pending' if outbound_qq.get('state') == 'retry' else 'no_new_content')
                records = [{k:x[k] for k in ('source_id','revision','summary')} for x in items]
                db.execute('INSERT INTO qq_memory_reads(read_at,keyword,status,records_json) VALUES (?,?,?,?)',
                    (max(x['read_at'] for x in items) if items else clock(), keyword, status, json.dumps(records, ensure_ascii=False)))
                prior = prior or bool(items)
            db.execute('DELETE FROM qq_memory_reads WHERE id NOT IN (SELECT id FROM qq_memory_reads ORDER BY id DESC LIMIT 100)')
    if config.get('qq_snapshot'):
        outbound_bili = SyncWorker(bili, bili_source, LocalTransport(qq), interval_seconds=1800).run_once(force=force)
        count = write_snapshot(qq, config['qq_snapshot'])
    else:
        outbound_bili = {'state': 'skipped_autobiography', 'sent': 0}
        count = 0

    live_notes = None
    if native and config.get('watcher_database'):
        from .autobiography import refresh_live_notes
        live_notes = refresh_live_notes(native, config['watcher_database'],
            account_id=config['bilibili_account_id'], now=clock(), host_name=_host_name)

    autobiography_episodes = 0
    if config.get('autobiography_snapshot') and native and config.get('watcher_database'):
        from .autobiography import build_autobiography_episodes, write_autobiography_snapshot
        episodes = build_autobiography_episodes(
            native,
            config['watcher_database'],
            account_id=config['bilibili_account_id'],
            now=clock(),
            host_name=_host_name,
        )
        autobiography_episodes = write_autobiography_snapshot(
            episodes,
            config['autobiography_snapshot'],
            now=clock(),
        )

    presence_updated = False
    if config.get('watcher_database'):
        from .autobiography import write_presence_snapshot, prune_bili_databases
        if config.get('presence_snapshot'):
            write_presence_snapshot(config['watcher_database'], config['presence_snapshot'], now=clock())
            presence_updated = True
        write_presence_snapshot(config['watcher_database'], state.parent / 'presence.json', now=clock())

        prune_marker = state / 'bili_prune.json'
        last_prune = 0.0
        if prune_marker.exists():
            try:
                last_prune = float(json.loads(prune_marker.read_text(encoding='utf-8')).get('last_at', 0.0))
            except (OSError, ValueError, TypeError):
                last_prune = 0.0
        if force or clock() - last_prune >= 86400:
            prune_bili_databases(
                native,
                config['watcher_database'],
                state / 'bilibili_bridge.sqlite3',
                account_id=config['bilibili_account_id'],
                now=clock(),
            )
            prune_marker.write_text(json.dumps({'last_at': clock()}), encoding='utf-8')

    return {'qq':outbound_qq,'bilibili':outbound_bili,'qq_context_records':count,
            'live_notes':live_notes,
            'autobiography_episodes':autobiography_episodes,'presence_updated':presence_updated,
            'compression':'deterministic','model_calls':0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    try:
        result = tick(json.loads(Path(args.config).read_text(encoding='utf-8')), force=args.force)
    except Exception as exc:
        print(json.dumps({'state':'error','error_type':type(exc).__name__}))
        raise SystemExit(1)
    print(json.dumps(result))
    if any(result[key]['state'] == 'retry' for key in ('qq','bilibili')):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
