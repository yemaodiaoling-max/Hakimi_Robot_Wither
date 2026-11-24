import json
import math


class Scheduler:
    def __init__(self, database, priority_room):
        self.database, self.room = database, str(priority_room)
        with database.connect(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS scheduler (id INTEGER PRIMARY KEY, value TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS seen_events (id TEXT PRIMARY KEY)")
            db.execute("INSERT OR IGNORE INTO scheduler VALUES (1,?)", (json.dumps({
                "state": "IDLE", "video": None, "bookmark": None, "live_session": None,
                "watch_epoch": 0, "status_seq": -1, "status_at": -1, "offline_count": 0,
                "room_id": self.room,
                "stream_seq": -1,
            }),))

    def snapshot(self):
        with self.database.connect() as db:
            return json.loads(db.execute("SELECT value FROM scheduler WHERE id=1").fetchone()[0])

    def apply(self, event):
        with self.database.connect(write=True) as db:
            if not db.execute("INSERT OR IGNORE INTO seen_events VALUES (?)", (event['id'],)).rowcount:
                return json.loads(db.execute("SELECT value FROM scheduler WHERE id=1").fetchone()[0])
            s = json.loads(db.execute("SELECT value FROM scheduler WHERE id=1").fetchone()[0])
            kind = event['type']
            if kind == 'live_status' and str(event['room_id']) == self.room:
                seq, at = int(event['sequence']), float(event['at'])
                received = float(event.get('received_at', at))
                if not all(math.isfinite(x) for x in (at, received)):
                    raise ValueError('invalid status time')
                if seq > s['status_seq'] and at >= s['status_at'] and 0 <= received-at <= 90:
                    s['status_seq'], s['status_at'] = seq, at
                    status = event['status']
                    if status == 'online':
                        session = str(event['session_id'])
                        if session != s['live_session']:
                            if s['video']:
                                s['bookmark'], s['video'] = s['video'], None
                            s['watch_epoch'] += 1
                            s['stream_seq'] = -1
                        s.update(state='WATCHING_PRIORITY_LIVE', live_session=session, offline_count=0)
                    elif status == 'offline' and s['live_session']:
                        s['offline_count'] += 1
                        if s['offline_count'] >= 2:
                            s['watch_epoch'] += 1
                            s.update(live_session=None, offline_count=0, video=s['bookmark'], bookmark=None)
                            s['state'] = 'WATCHING_VIDEO' if s['video'] else 'BROWSING'
                    else:
                        # Unknown/error interrupts the consecutive offline confirmations.
                        s['offline_count'] = 0
            elif kind in {'stream_lost', 'stream_restored', 'progress'}:
                if event.get('watch_epoch') == s['watch_epoch']:
                    if kind == 'progress' and s['video'] and event.get('bvid') == s['video']['bvid']:
                        s['video']['position'] = max(s['video']['position'], float(event['position']))
                    elif (event.get('session_id') == s['live_session'] and s['live_session']
                          and int(event['sequence']) > s['stream_seq']):
                        s['stream_seq'] = int(event['sequence'])
                        s['state'] = 'RECOVERING' if kind == 'stream_lost' else 'WATCHING_PRIORITY_LIVE'
            elif kind == 'video' and not s['live_session'] and event.get('watch_epoch') == s['watch_epoch']:
                if not math.isfinite(float(event.get('position', 0))) or float(event.get('position', 0)) < 0:
                    raise ValueError('negative video position')
                s['watch_epoch'] += 1
                s['video'] = {key: event[key] for key in ('bvid', 'cid')}
                s['video']['position'] = float(event.get('position', 0))
                s['state'] = 'WATCHING_VIDEO'
            db.execute("UPDATE scheduler SET value=? WHERE id=1", (json.dumps(s),))
            return s

    async def run(self, events):
        async for event in events:
            self.apply(event)
