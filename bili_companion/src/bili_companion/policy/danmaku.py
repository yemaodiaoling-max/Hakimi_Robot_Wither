from bili_companion.domain import Scope
import math


class ActionExecutor:
    """Persist dispatch intent before transport; ambiguous outcomes never retry."""
    def __init__(self, database, memory, config):
        self.database, self.memory, self.config = database, memory, config
        with database.connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS danmaku_attempts (
                account TEXT, room TEXT, session TEXT, dedup TEXT, text TEXT,
                at REAL, state TEXT, PRIMARY KEY(account,room,session,dedup))""")

    def recent(self, account_id, room_id, *, limit=8):
        """Hakimi's own latest danmaku in this room, newest last, across sessions."""
        with self.database.connect() as db:
            rows = db.execute("""SELECT text, at FROM danmaku_attempts WHERE account=? AND room=?
                AND state IN ('sent','preview','dispatching','unknown') ORDER BY at DESC LIMIT ?""",
                (account_id, room_id, max(1, min(20, int(limit))))).fetchall()
        return [dict(text=row['text'], at=row['at']) for row in reversed(rows)]

    def preview(self, candidate, current, *, account_id, watch_epoch, now, authenticated=True):
        if not self.config.get('dry_run', True):
            return 'sender_not_implemented'
        return self._admit(candidate,current,account_id=account_id,watch_epoch=watch_epoch,
                           now=now,authenticated=authenticated,record_state='preview')

    def _admit(self, candidate, current, *, account_id, watch_epoch, now, authenticated, record_state):
        if not math.isfinite(now):
            return 'invalid_time'
        if candidate.room_id not in self.config['allowed_rooms']:
            return 'room_not_allowed'
        if (current.get('room_id') != candidate.room_id
                or current['state'] != 'WATCHING_PRIORITY_LIVE' or current['watch_epoch'] != watch_epoch
                or candidate.watch_epoch != watch_epoch
                or current['live_session'] != candidate.session_id):
            return 'inactive_session'
        if not authenticated:
            return 'auth_required'
        if not candidate.text.strip() or len(candidate.text) > 30 or not candidate.dedup_key:
            return 'invalid_text'
        if now >= candidate.expires_at:
            return 'expired'
        if not candidate.evidence_ids:
            return 'missing_evidence'
        scope = Scope(account_id, 'live_room', candidate.room_id)
        for evidence_id in candidate.evidence_ids:
            evidence = self.memory.get(evidence_id, scope)
            if (not evidence or evidence['session_id'] != candidate.session_id
                    or evidence.get('attribution')!='observation' or evidence.get('confidence',0)<.8
                    or not 0 <= now-evidence['observed_at'] <= self.config['evidence_max_age_seconds']
                    or candidate.expires_at > evidence['observed_at'] + self.config['evidence_max_age_seconds']):
                return 'stale_evidence'
        with self.database.connect(write=True) as db:
            prior = db.execute("""SELECT * FROM danmaku_attempts WHERE account=? AND room=?
                ORDER BY at DESC""", (account_id, candidate.room_id)).fetchall()
            if any(row['session'] == candidate.session_id and
                   (row['dedup'] == candidate.dedup_key or row['text'] == candidate.text) for row in prior):
                return 'duplicate'
            if prior and now-prior[0]['at'] < max(90,self.config['cooldown_seconds']):
                return 'cooldown'
            if sum(row['at'] > now-3600 for row in prior) >= min(12,self.config['hourly_danmaku_limit']):
                return 'hourly_limit'
            db.execute("INSERT INTO danmaku_attempts VALUES (?,?,?,?,?,?,?)", (
                account_id, candidate.room_id, candidate.session_id, candidate.dedup_key, candidate.text, now,record_state))
            return 'dry_run' if record_state=='preview' else 'admitted'

    def dispatch(self, candidate, current_provider, sender, *, account_id, now, authenticated=True, clock=None):
        if clock is not None:now=clock()
        if candidate.room_id not in self.config.get('allowed_rooms', ()):
            return 'room_not_allowed'
        current=current_provider()
        if self.config.get('dry_run',True):
            return self.preview(candidate,current,account_id=account_id,
                watch_epoch=candidate.watch_epoch,now=now,authenticated=authenticated)
        result=self._admit(candidate,current,account_id=account_id,watch_epoch=candidate.watch_epoch,
            now=now,authenticated=authenticated,record_state='dispatching')
        if result!='admitted':
            return result
        # A recovered dispatching record also blocks retries after process death.
        latest=current_provider()
        if clock is not None and clock()>=candidate.expires_at:
            status='expired'
        elif (latest.get('watch_epoch')!=candidate.watch_epoch
                or latest.get('live_session')!=candidate.session_id
                or latest.get('state')!='WATCHING_PRIORITY_LIVE'
                or latest.get('room_id')!=candidate.room_id):
            status='cancelled'
        else:
            try:
                response=sender(candidate.room_id,candidate.text)
                status=response.get('status') if isinstance(response,dict) else response.status
                if status not in {'sent','rejected','unknown'}:
                    status='unknown'
            except Exception:
                status='unknown'
        with self.database.connect(write=True) as db:
            db.execute('''UPDATE danmaku_attempts SET state=? WHERE account=? AND room=?
                AND session=? AND dedup=? AND state='dispatching' ''',
                (status,account_id,candidate.room_id,candidate.session_id,candidate.dedup_key))
        return status
