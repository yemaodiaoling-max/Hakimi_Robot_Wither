"""MemoryStore reads of single records and graph views."""
import re
import sqlite3
import time
from hakimi.memory.store_base import content_hash
from hakimi.memory.text import terms


class StoreReadMixin:
    """Mixin of MemoryStore; see memory_store."""

    # ------------------------------------------------------------------- read
    def read(self, memory_id_value, session, users, *, cross_groups=False, offset=0, limit=3000, now=None, source='note'):
        if source not in {'note', 'message'}:
            raise ValueError("source must be note or message")
        raw_id = str(memory_id_value or "").strip()
        if re.fullmatch(r"(?:L[1-9][0-9]{0,19}|LM[0-9a-f]{24}|R[1-9][0-9]{0,19}-[gm][0-9-]+)",
                        raw_id, re.IGNORECASE):
            if source == 'message':
                return None
            from hakimi.social.identity_legacy import read_legacy
            return read_legacy(self.base, raw_id, session, cross_groups=cross_groups,
                               offset=offset, limit=limit, now=now)
        self.sync()
        now = time.time() if now is None else now
        scope, params = self.scope(session, users, cross_groups, include_inactive=True)
        mid = str(memory_id_value).removeprefix("M")
        if not mid.isdigit():
            return None
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = max(1, min(int(limit or 3000), 6000))
        except (TypeError, ValueError):
            limit = 3000
        with self.db() as db:
            row = db.execute("SELECT r.* FROM records r WHERE r.id=? AND " + scope, [int(mid), *params]).fetchone()
            if not row:
                return None
            record = dict(row)
            if row['channel']=='voice':
                from hakimi.memory.voice_receipts import read as read_voice
                voice=read_voice(self.base,session,row['origin'],offset,limit)
                if voice.get('error'):return None
                return {'id':'M'+mid,'kind':'chat','channel':'voice','session_key':session,'user_id':row['user_id'],'message_id':row['origin'],'text':voice['text'],'text_kind':'automatic_transcription','verification':'asr_unverified','status':'active','time':row['at'],'expires_at':row['expires'],'raw_available':True,'source':'QQ语音自动识别，可能有误','source_view':source,'offset':voice['offset'],'total_chars':voice['total_chars'],'next_offset':voice['offset']+len(voice['text']) if voice['more'] else None}
            original = db.execute("SELECT * FROM memory_originals WHERE record_id=? ORDER BY id DESC LIMIT 1",
                                  (row["id"],)).fetchone()
            event_card = None
            if row['channel'] == 'daily_events':
                event_card = db.execute('SELECT observed_at,recorded_at,event_time FROM daily_event_cards WHERE record_id=?',
                                        (row['id'],)).fetchone()
            relations = [dict(edge) for edge in db.execute("SELECT subject,relation,object FROM edges WHERE record_id=?", (row["id"],))]
            original_message = db.execute("SELECT r.text,r.at FROM records r WHERE r.kind='chat' AND r.session=? AND r.origin=? AND " + scope,
                [row["session"], row["origin"], *params]).fetchone()
            newer = None
            if row['superseded_by']:
                newer = db.execute("SELECT id FROM records WHERE id=?", (row['superseded_by'],)).fetchone()
            if newer is None:
                newer = db.execute("SELECT id FROM records WHERE kind=? AND key=? AND key!='' AND (session=? OR kind='person' AND shared=1 AND ?=1) AND user_id=? AND (at>? OR at=? AND id>?) ORDER BY at DESC,id DESC LIMIT 1",
                    (row['kind'], row['key'], row['session'], row['shared'], row['user_id'], row['at'], row['at'], row['id'])).fetchone()
        source_text = original['text'] if original else (record['text'] or record['summary'])
        if source == 'message':
            source_text = original_message[0] if original_message else ''
        if row['state'] in {'archived', 'retracted'}:
            status = row['state']
        elif newer:
            status = 'superseded'
        elif row['expires'] and row['expires'] <= now:
            status = 'expired'
        else:
            status = row['state']
        text = str(source_text or '')
        text_view = text[offset:offset + limit]
        next_offset = offset + len(text_view) if offset + len(text_view) < len(text) else None
        if source == 'message':
            text_kind = 'raw_chat'
        elif original:
            text_kind = 'legacy_note' if row['legacy'] else 'manual_original'
        elif row['kind'] == 'chat' and text:
            text_kind = 'raw_chat'
        elif row['kind'] == 'chat':
            text_kind = 'knowledge_note'
        else:
            text_kind = 'knowledge_note'
        digest = content_hash(text) if source == 'message' else ((original['content_hash'] if original else row['content_hash']) or content_hash(text))
        return {
            "status": status, "superseded_by": 'M' + str(newer[0]) if newer else None,
            "verification": row['verification'], "evidence": row['evidence'], "expires_at": row['expires'],
            "id": "M" + mid, "kind": row["kind"], "session_key": row["session"], "user_id": row["user_id"],
            "time": row["at"], "message_id": row["origin"], "text": text_view,
            "observed_at": event_card['observed_at'] if event_card else row['at'] if row['kind'] == 'chat'
                           else original_message['at'] if original_message and original_message['text'] else None,
            "recorded_at": event_card['recorded_at'] if event_card else original['created_at'] if original else row['at'],
            "event_time": event_card['event_time'] if event_card else '',
            "text_kind": text_kind, "hash": digest, "canonical_hash": digest,
            "original_id": original['id'] if original else row['original_id'],
            "provider": original['provider'] if original else row['model'],
            "channel": original['channel'] if original else row['channel'],
            "legacy": bool(row['legacy']),
            "raw_available": bool(row['text']) if row['kind'] == 'chat' else bool(original_message and original_message[0]),
            "manual_original": bool(original),
            "source": row["source"], "relations": relations,
            "source_view": source,
            "source_message_chars": len(original_message[0] or "") if original_message else 0,
            "offset": offset, "limit": limit, "total_chars": len(text), "returned_chars": len(text_view),
            "next_offset": next_offset, "has_more": next_offset is not None,
            "excerpted": len(text_view) < len(text),
            "supersedes": ('M' + str(row['supersedes_id'])) if row['supersedes_id'] else None,
            "state": row['state'],
        }

    def visible_record_ids(self, session, users, ids, *, cross_groups=False):
        """Return the subset of numeric/M ids readable in the current scope."""
        values = []
        for value in ids:
            text = str(value).upper().removeprefix('M')
            if text.isdigit() and int(text) > 0:
                values.append(int(text))
        if not values:
            return set()
        scope, params = self.scope(session, users, cross_groups, include_inactive=True)
        marks = ','.join('?' for _ in values)
        with self.db() as db:
            rows = db.execute(f"SELECT r.id FROM records r WHERE r.id IN ({marks}) AND " + scope,
                              [*values, *params]).fetchall()
        return {int(row[0]) for row in rows}

    def related_manual_ids(self, session, text, users=(), *, cross_groups=False, now=None, limit=3):
        now = time.time() if now is None else now
        query_terms = terms(str(text))
        if not query_terms:
            return []
        scope, params = self.scope(session, users, cross_groups)
        expression = " OR ".join('"' + word + '"' for word in sorted(query_terms)[:32])
        try:
            with self.db() as db:
                rows = db.execute(
                    "SELECT r.id FROM search JOIN records r ON r.id=search.rowid WHERE search MATCH ? AND " + scope +
                    " AND (r.expires=0 OR r.expires>?) AND r.original_id IS NOT NULL ORDER BY r.at DESC LIMIT ?",
                    [expression, *params, now, max(1, min(int(limit), 8))],
                ).fetchall()
            return ['M' + str(row[0]) for row in rows]
        except sqlite3.Error:
            return []

    # ------------------------------------------------------------------ graph
    def rebuild_graph(self, limit=256, *, full=False):
        from hakimi.memory.graph_index import index_record
        with self.db() as db:
            if full:
                db.execute("INSERT OR REPLACE INTO memory_meta VALUES ('graph_cursor','0')")
            cursor = db.execute("SELECT value FROM memory_meta WHERE key='graph_cursor'").fetchone()
            after = int(cursor[0]) if cursor else 0
            rows = db.execute("SELECT * FROM records WHERE id>? AND state!='deleted' ORDER BY id LIMIT ?",
                              (after, max(1, min(int(limit), 2000)))).fetchall()
            for row in rows:
                index_record(db, dict(row))
            if rows:
                db.execute("INSERT OR REPLACE INTO memory_meta VALUES ('graph_cursor',?)", (str(rows[-1]['id']),))
        return len(rows)

    def graph(self, session, users=(), **kwargs):
        from hakimi.memory.memory_graph import graph_data
        return graph_data(self, session=session, users=users, **kwargs)
