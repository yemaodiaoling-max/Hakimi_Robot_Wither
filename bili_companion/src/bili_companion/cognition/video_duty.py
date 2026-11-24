"""Share video provider backoff across the watcher and QQ workers."""
from .gateway import CognitionUnavailable


class VideoDuty:
    def __init__(self, database, purpose, profile_path):
        self.database = database
        self.key = (purpose, str(profile_path))
        with database.connect(write=True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS video_provider_duty (
                purpose TEXT, profile TEXT, generation INTEGER, failures INTEGER, retry_at REAL,
                PRIMARY KEY(purpose, profile))''')
            db.execute('INSERT OR IGNORE INTO video_provider_duty VALUES (?,?,0,0,0)', self.key)

    def _state(self, db):
        return db.execute('SELECT * FROM video_provider_duty WHERE purpose=? AND profile=?', self.key).fetchone()

    def on_standby(self, since=0):
        with self.database.connect() as db:
            return self._state(db)['retry_at'] > since

    @staticmethod
    def standby_for(database, purpose, profile_path):
        with database.connect() as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE name='video_provider_duty'").fetchone()
            return bool(table and db.execute('''SELECT 1 FROM video_provider_duty
                WHERE purpose=? AND profile=? AND retry_at>0''', (purpose, str(profile_path))).fetchone())

    def admit(self, now):
        with self.database.connect(write=True) as db:
            state = self._state(db)
            generation = state['generation']
            retry_at = state['retry_at']
            if not retry_at:
                return generation, False
            if retry_at > now:
                return None
            # One real request can attempt recovery. A dead worker's claim expires.
            generation += 1
            db.execute('''UPDATE video_provider_duty SET generation=?, retry_at=?
                          WHERE purpose=? AND profile=?''', (generation, now + 300, *self.key))
            return generation, True

    def finish(self, ticket, now, error=None):
        generation, probe = ticket
        code = getattr(error, 'code', None)
        provider_failure = isinstance(error, CognitionUnavailable) and code not in {
            'provider_input_bound_exceeded', 'provider_media_conversion_failed', 'stale_child_epoch',
        }
        if error is not None and not provider_failure and not probe:
            return
        unrouted = (code == 'provider_model_unrouted' or
                    getattr(error, 'provider_code', None) == 'provider_model_unrouted')
        pool_empty = getattr(error, 'cause_code', code) == 'upstream_pool_unavailable'
        with self.database.connect(write=True) as db:
            state = self._state(db)
            if state['generation'] != generation:
                return
            failures = state['failures']
            retry_at = 0
            if error is None:
                failures = 0
            elif probe:
                failures = min(failures + int(provider_failure), 8)
                delay = min(60 * 2 ** failures, 900) if provider_failure else 60
                if unrouted:
                    delay = min(900 * 2 ** (failures - 1), 3600)
                retry_at = now + delay
            elif provider_failure:
                failures += 1
                if failures >= 3 or unrouted or pool_empty:
                    failures = 0
                    retry_at = now + (900 if unrouted else 60)
                    generation += 1
            if failures != state['failures'] or retry_at != state['retry_at']:
                db.execute('''UPDATE video_provider_duty SET generation=?, failures=?, retry_at=?
                              WHERE purpose=? AND profile=?''', (generation, failures, retry_at, *self.key))
