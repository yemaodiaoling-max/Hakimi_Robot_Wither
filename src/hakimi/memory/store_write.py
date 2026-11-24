"""MemoryStore write paths: observed chat, automatic notes, dialogue signatures, manual notes with immutable originals."""
import hashlib
import json
import math
import re
import sqlite3
import struct
import time
from hakimi.memory.lifecycle_rules import discussion_episode, expanded_terms, explicit_person, sourced_reply
from hakimi.memory.store_base import _summary, content_hash, cosine, index_text, key_name, vectorize
from hakimi.memory.text import terms


class StoreWriteMixin:
    """Mixin of MemoryStore; see memory_store."""

    # ------------------------------------------------------------- write paths
    def _put(self, db, row, kind):
        session = str(row.get("session_key", ""))
        if not re.fullmatch(r"(?:group|private):[1-9][0-9]{0,19}|self:bilibili", session):
            return
        is_chat = kind == "chat"
        text = row.get("text" if is_chat else "note", "")
        if not isinstance(text, str) or not text.strip():
            return
        at = row.get("received_at", row.get("created_at", 0))
        if type(at) not in (int, float) or not math.isfinite(at) or at <= 0:
            return
        user = str(row.get("user_id" if is_chat else "person_id", ""))
        origin = str(row.get("message_id" if is_chat else "origin_message_id", ""))
        key = key_name(row.get("key", ""))
        source = str(row.get("source", ""))
        evidence = str(row.get('evidence', ''))[:520]
        if not is_chat and not evidence and origin:
            original = db.execute("SELECT text FROM records WHERE session=? AND origin=? AND kind='chat' LIMIT 1", (session, origin)).fetchone()
            if original:
                evidence = original[0][:520]
        if kind == "lore":
            from hakimi.memory.lifecycle_rules import source_url
            source = source_url(source)
            if not source:
                return
        if not is_chat and kind != 'dialogue':
            same = db.execute("SELECT id,at FROM records WHERE kind=? AND session=? AND user_id=? AND key=? AND text=? AND source=? AND state='active' ORDER BY at DESC LIMIT 1",
                              (kind, session, user, key, text, source)).fetchone()
            if same and at <= same['at']:
                return
            if same:
                # Durable person/preference/lore/episode/reflection rows have no
                # age-based expiry.  Reconfirming the current version must not
                # introduce one either; corrections still create new versions.
                newer = db.execute("SELECT 1 FROM records WHERE kind=? AND session=? AND user_id=? AND key=? AND at>? LIMIT 1", (kind, session, user, key, same['at'])).fetchone()
                if not newer:
                    keep_expires = (
                        float(row["expires_at"])
                        if row.get("channel") == "bilibili_watch" and float(row.get("expires_at") or 0) > 0
                        else 0
                    )
                    db.execute("UPDATE records SET expires=?,at=?,evidence=CASE WHEN ?!='' THEN ? ELSE evidence END WHERE id=?",
                               (keep_expires, at, str(row.get('evidence', ''))[:520], str(row.get('evidence', ''))[:520], same['id']))
                    return
        source_key = "chat:" + session + ":" + origin if is_chat and origin else hashlib.sha256(
            json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        # Raw chat is short-lived transport evidence and dialogue is an explicit
        # 30-minute cache.  Every durable memory kind keeps the historical state
        # without a calendar expiry; contradictions/supersession decide staleness.
        if is_chat or kind == 'dialogue':
            expires = 0 if is_chat else min(
                float(row.get("expires_at", at + 1800) or (at + 1800)), at + 1800)
        elif row.get("channel") == "bilibili_watch" and float(row.get("expires_at") or 0) > 0:
            expires = float(row["expires_at"])
        else:
            expires = 0
        shared = int(bool(row.get('shared', kind == 'lore' and session.startswith('group:'))))
        if kind == 'person' and session.startswith('group:'):
            shared = 1
        verification = str(row.get('verification', 'model_note' if not is_chat else 'raw'))
        digest = content_hash(text)
        targets = row.get('direct_targets', '')
        if isinstance(targets, (list, tuple)):
            targets = json.dumps([str(item) for item in targets if re.fullmatch(r'[1-9][0-9]{0,19}', str(item))], ensure_ascii=False)
        elif not isinstance(targets, str):
            targets = ''
        reply_to = str(row.get('reply_to', '') or '')
        self_id = str(row.get('self_id', '') or '')
        nickname = str(row.get('nickname', '') or '')[:80]
        topics = row.get('topics', '')
        if isinstance(topics, (list, tuple)):
            topics = json.dumps([str(item)[:40] for item in topics[:8]], ensure_ascii=False)
        elif not isinstance(topics, str):
            topics = ''
        result = db.execute(
            "INSERT OR IGNORE INTO records(source_key,kind,session,user_id,key,text,at,expires,origin,source,"
            "summary,evidence,verification,shared,state,content_hash,channel,model,nickname,direct_targets,"
            "reply_to,self_id,topics) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_key, kind, session, user, key, text, at, expires, origin, source,
             _summary(text), evidence, verification, shared, str(row.get('state', 'active')), digest,
             str(row.get('channel', '') or 'auto'), str(row.get('provider', '') or ''), nickname,
             targets, reply_to, self_id, topics),
        )
        if not result.rowcount:
            return
        rid = result.lastrowid
        if is_chat and row.get('channel')=='voice':return
        try:
            tokens = self._insert_search(db, rid, text, key)
        except sqlite3.Error:
            tokens = expanded_terms(index_text(text) + ' ' + key, lambda value: terms(value, limit=32000))
        relations = [("record:" + str(rid), "in", session)]
        if re.fullmatch(r"[1-9][0-9]{0,19}", user):
            relations.append(("person:" + user, "said" if is_chat else key or kind, "record:" + str(rid)))
        for pair in sorted(set(re.findall(r"《([^《》\n]{2,24})》|#([A-Za-z0-9_\u4e00-\u9fff]{2,24})", text)))[:8]:
            relations.append(("record:" + str(rid), "topic", next(part for part in pair if part)))
        if key and kind != "person":
            relations.append(("record:" + str(rid), "topic", key))
        db.executemany("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)", [(rid, *edge) for edge in relations])
        db.execute("UPDATE records SET summary=?,vector=? WHERE id=?",
                   (_summary(text), vectorize(tokens), rid))
        if not is_chat and row.get('original_id') is None and self._legacy_manual_row(row):
            self._ensure_legacy_original(db, rid, row, source_key, digest)
        try:
            from hakimi.memory.graph_index import index_record
            record = {
                'id': rid, 'session': session, 'kind': kind, 'text': text, 'summary': _summary(text),
                'user_id': user, 'key': key, 'source': source, 'at': at, 'nickname': nickname,
                'direct_targets': targets, 'reply_to': reply_to, 'self_id': self_id,
                'origin': origin,
                'supersedes_id': row.get('supersedes_id') if type(row.get('supersedes_id')) is int else None,
                'topics': topics,
            }
            index_record(db, record)
        except (sqlite3.Error, ValueError, TypeError):
            pass
        if is_chat:
            proposal = explicit_person(row)
            if proposal:
                db.execute('INSERT OR IGNORE INTO edges VALUES (?,?,?,?)',
                           (rid, 'person:' + user, 'self:' + proposal['key'], proposal['note']))
                self._put(db, proposal, 'person')

    def _ensure_legacy_original(self, db, rid, row, source_key, digest):
        exists = db.execute("SELECT id FROM memory_originals WHERE record_id=?", (rid,)).fetchone()
        if exists:
            db.execute("UPDATE records SET original_id=?,legacy=? WHERE id=?",
                       (exists[0], int(not bool(row.get('original_exact'))), rid))
            return exists[0]
        exact = bool(row.get('original_exact')) and row.get('content_hash') == digest
        metadata = json.dumps({
            'legacy': not exact,
            'topics': row.get('topics', []) if isinstance(row.get('topics'), list) else [],
            'related_memory_ids': row.get('related_memory_ids', []) if isinstance(row.get('related_memory_ids'), list) else [],
            'origin_message_id': str(row.get('origin_message_id', '')),
            'request_id': str(row.get('request_id', '')),
        }, ensure_ascii=False)
        cursor = db.execute(
            "INSERT INTO memory_originals(record_id,request_id,content_hash,text,kind,session,user_id,item_key,"
            "source,evidence,provider,channel,verification,metadata,created_at,legacy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, str(row.get('request_id') or ("legacy:" + str(source_key))), digest, row.get('note', ''),
             row.get('kind', 'preference'), row.get('session_key', ''), str(row.get('person_id', '') or ''),
             str(row.get('key', '')), str(row.get('source', '')), str(row.get('evidence', ''))[:520],
             str(row.get('provider', '')), str(row.get('channel', '') or 'legacy'),
             str(row.get('verification', 'legacy')), metadata, float(row.get('created_at', time.time())),
             int(not exact)),
        )
        db.execute("UPDATE records SET original_id=?,legacy=? WHERE id=?",
                   (cursor.lastrowid, int(not exact), rid))
        return cursor.lastrowid

    def observe(self, row):
        with self.db() as db:
            self._put(db, row, "chat")

    def note(self, row):
        with self.db() as db:
            self._put(db, row, row.get("kind", "preference"))
        with self.topic_lock:
            self.topic_cache.clear()

    def topic_signature(self, session, text):
        """Project onto shared knowledge/graph nodes; no model or full-memory read."""
        text = index_text(str(text))[:800]
        tokens = terms(text)
        if len(tokens) < 2:
            return []
        key = (session, text)
        with self.topic_lock:
            cached = self.topic_cache.get(key)
            if cached and time.monotonic() - cached[0] < 120:
                self.topic_cache.move_to_end(key)
                return list(cached[1])
        expression = ' OR '.join('"' + word + '"' for word in sorted(tokens)[:32])
        query = dict(struct.iter_unpack('<Hf', vectorize(tokens)))
        scored = []
        with self.db() as db:
            rows = db.execute("SELECT r.* FROM search JOIN records r ON r.id=search.rowid WHERE search MATCH ? "
                "AND r.state='active' AND (r.expires=0 OR r.expires>?) AND (r.kind='lore' AND (r.session=? OR r.shared=1 AND r.session LIKE 'group:%') OR "
                "r.session=? AND (r.kind IN ('episode','dialogue') OR r.kind='chat' AND EXISTS "
                "(SELECT 1 FROM edges e WHERE e.record_id=r.id AND e.relation='topic'))) ORDER BY bm25(search) LIMIT 12",
                (expression, time.time(), session, session))
            for row in rows:
                overlap = len(tokens & terms(index_text(row['text'] or row['summary']), limit=8000))
                similarity = cosine(row['vector'], query)
                if overlap >= 2 and similarity >= .08:
                    scored.append((similarity + overlap / max(4, len(tokens)), dict(row)))
            result = []
            for _, row in sorted(scored, key=lambda item: item[0], reverse=True)[:2]:
                result.append('d' + hashlib.sha256(row['source_key'].encode()).hexdigest()[:15])
                for edge in db.execute("SELECT object FROM edges WHERE record_id=? AND relation='topic' LIMIT 2", (row['id'],)):
                    result.append('e' + hashlib.sha256(edge[0].encode()).hexdigest()[:15])
        result = list(dict.fromkeys(result))[:6]
        with self.topic_lock:
            self.topic_cache[key] = (time.monotonic(), result)
            while len(self.topic_cache) > 128:
                self.topic_cache.popitem(last=False)
        return result

    def dialogue_signature(self, session, trigger, question, reply, now):
        if len(terms(reply)) < 3:
            return []
        entry = {'session_key': session, 'kind': 'dialogue', 'note': question[:240] + '\n' + reply[:800],
                 'key': trigger, 'created_at': now, 'expires_at': now + 1800}
        self.note(entry)
        for note in sourced_reply(session, trigger, question, reply, now):
            self.note(note)
        episode = discussion_episode(session, trigger, question, reply, now)
        if episode:
            self.note(episode)
        return self.topic_signature(session, reply[:400] + ' ' + question[:240])

    def remember_manual(self, proposal, *, session, provider='gemini', channel='', now=None,
                        origin_message_id=''):
        """Persist an exact manual original and its mutable index entry.

        Returns a receipt dictionary.  A retry of the current request returns the
        same record id; a different body with the same stable key creates a new
        version linked by supersedes/superseded_by.
        """
        now = time.time() if now is None else float(now)
        kind = str(proposal.get('kind', 'preference'))
        note = proposal.get('note')
        if not isinstance(note, str) or not note.strip():
            return {'saved': False, 'error': 'note_empty'}
        user = str(proposal.get('user_id', '') or '')
        key = key_name(str(proposal.get('key', '') or '').strip())
        source = str(proposal.get('source', '') or '')
        evidence = str(proposal.get('evidence', '') or '')[:240]
        topics = proposal.get('topics') if isinstance(proposal.get('topics'), list) else []
        topics = [str(item).strip() for item in topics if isinstance(item, str) and 1 <= len(str(item).strip()) <= 40][:8]
        related = proposal.get('related_memory_ids') if isinstance(proposal.get('related_memory_ids'), list) else []
        related = [str(item).upper().removeprefix('M') for item in related if str(item).upper().removeprefix('M').isdigit()][:8]
        digest = content_hash(note)
        # Stable event/turn origin distinguishes a retry from a deliberate
        # reversion.  Callers without an origin still get a unique write token;
        # their immediate duplicate is caught by the latest-text branch below.
        origin_key = str(origin_message_id) or f"auto:{now}:{time.time_ns()}"
        request_id = hashlib.sha256(json.dumps({
            'session': session, 'kind': kind, 'user': user, 'key': key, 'note': note,
            'source': source, 'evidence': evidence, 'topics': topics,
            'related': related, 'origin': origin_key,
        }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self.db() as db:
            # Hold a RESERVED lock across the lookup+insert.  A retried tool call
            # and a concurrent writer therefore cannot both create a new original.
            db.execute("BEGIN IMMEDIATE")
            # Exact request replay wins even when a later correction has already
            # superseded it.  A genuinely new origin (event/turn) has a different
            # request id below and may intentionally revert the value.
            existing = db.execute(
                "SELECT r.*,o.id AS original_row_id FROM memory_originals o JOIN records r ON r.id=o.record_id "
                "WHERE o.request_id=? ORDER BY r.id ASC LIMIT 1", (request_id,),
            ).fetchone()
            if existing:
                original = db.execute("SELECT * FROM memory_originals WHERE id=?",
                                      (existing['original_row_id'],)).fetchone()
                return self._receipt(db, existing, original, created=False, duplicate=True)
            latest = db.execute(
                "SELECT * FROM records WHERE kind=? AND session=? AND user_id=? AND key=? AND state='active' "
                "AND original_id IS NOT NULL ORDER BY at DESC,id DESC LIMIT 1",
                (kind, session, user, key),
            ).fetchone()
            if latest and latest['text'] == note and str(latest['source']) == source:
                original = db.execute("SELECT * FROM memory_originals WHERE record_id=? ORDER BY id DESC LIMIT 1",
                                      (latest['id'],)).fetchone()
                return self._receipt(db, latest, original, created=False, duplicate=True)
            supersedes = latest['id'] if latest is not None and key and kind != 'chat' else None
            verification = self._verification(kind, evidence, source)
            shared = 1 if kind in {'person', 'lore'} and session.startswith('group:') else 0
            entry = {
                'kind': kind, 'session_key': session, 'key': key, 'note': note, 'source': source,
                'evidence': evidence, 'created_at': now, 'provider': provider,
                'channel': channel or provider, 'origin_message_id': str(origin_message_id),
                'verification': verification, 'shared': shared, 'topics': topics,
                'related_memory_ids': ['M' + item for item in related], 'request_id': request_id,
                'content_hash': digest, 'original_exact': True, 'legacy': 0,
                'person_id': user if kind == 'person' else '',
                'supersedes_id': int(supersedes or 0),
            }
            metadata = json.dumps({
                'topics': topics, 'related_memory_ids': ['M' + item for item in related],
                'origin_message_id': str(origin_message_id), 'request_id': request_id,
            }, ensure_ascii=False)
            cursor = db.execute(
                "INSERT INTO memory_originals(record_id,request_id,content_hash,text,kind,session,user_id,item_key,"
                "source,evidence,provider,channel,verification,metadata,created_at,legacy) "
                "VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (request_id, digest, note, kind, session, user, key, source, evidence,
                 provider, channel or provider, verification, metadata, now),
            )
            original_id = cursor.lastrowid
            source_key = hashlib.sha256(json.dumps(entry, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            result = db.execute(
                "INSERT INTO records(source_key,kind,session,user_id,key,text,at,expires,origin,source,summary,"
                "evidence,verification,shared,state,original_id,content_hash,channel,model,supersedes_id,"
                "superseded_by,legacy,topics) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_key, kind, session, user, key, note, now, 0, str(origin_message_id), source,
                 _summary(note), evidence, verification, shared, 'active', original_id, digest,
                 channel or provider, provider, supersedes, None, 0, json.dumps(topics, ensure_ascii=False)),
            )
            rid = result.lastrowid
            db.execute("UPDATE memory_originals SET record_id=? WHERE id=?", (rid, original_id))
            tokens = expanded_terms(index_text(note) + ' ' + key, lambda value: terms(value, limit=32000))
            relations = [("record:" + str(rid), "in", session)]
            if re.fullmatch(r"[1-9][0-9]{0,19}", user):
                relations.append(("person:" + user, "refers_to", "record:" + str(rid)))
            for topic in topics[:8]:
                relations.append(("record:" + str(rid), "topic", topic))
            if key and kind != 'person':
                relations.append(("record:" + str(rid), "topic", key))
            db.executemany("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)", [(rid, *edge) for edge in relations])
            db.execute("UPDATE records SET vector=? WHERE id=?", (vectorize(tokens), rid))
            if supersedes:
                db.execute("UPDATE records SET state='superseded',superseded_by=? WHERE id=? AND state='active'",
                           (rid, supersedes))
            try:
                from hakimi.memory.graph_index import index_record
                record = {
                    'id': rid, 'session': session, 'kind': kind, 'text': note, 'summary': _summary(note),
                    'user_id': user, 'key': key, 'source': source, 'at': now, 'topics': json.dumps(topics, ensure_ascii=False),
                    'supersedes_id': supersedes, 'self_id': '',
                }
                index_record(db, record)
            except (sqlite3.Error, ValueError, TypeError):
                pass
        index_status = 'pending_fts'
        try:
            with self.db() as db:
                if self._insert_search(db, rid, note, key):
                    index_status = 'indexed'
        except sqlite3.Error:
            index_status = 'pending_fts'
        with self.db() as db:
            row = db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
            original = db.execute("SELECT * FROM memory_originals WHERE id=?", (original_id,)).fetchone()
            receipt = self._receipt(db, row, original, created=True, duplicate=False)
        receipt['index_status'] = index_status
        receipt['entry'] = entry
        receipt['record_text'] = note
        return receipt

    def _verification(self, kind, evidence, source):
        if kind == 'lore':
            return 'source_excerpt' if evidence else 'model_note'
        if kind == 'reflection':
            return 'subjective_reflection'
        if kind == 'episode':
            return 'model_note'
        return 'model_note'

    def _receipt(self, db, row, original, *, created, duplicate):
        rid = row['id']
        state = db.execute("SELECT record_id,model,content_hash,chunks,updated_at,error FROM embedding_state WHERE record_id=?",
                           (rid,)).fetchone()
        return {
            'saved': True, 'created': created, 'duplicate': duplicate,
            'id': 'M' + str(rid), 'record_id': rid,
            'original_id': original['id'] if original else row['original_id'],
            'hash': (original['content_hash'] if original else row['content_hash']) or content_hash(row['text']),
            'kind': row['kind'], 'session_key': row['session'], 'user_id': row['user_id'],
            'key': row['key'], 'state': row['state'], 'legacy': bool(row['legacy']),
            'index_status': 'indexed' if db.execute("SELECT 1 FROM search WHERE rowid=?", (rid,)).fetchone() else 'pending_fts',
            'embedding_status': 'indexed' if state and not state['error'] and state['content_hash'] == row['content_hash']
                                else 'pending' if not state else 'error',
            'embedding_model': state['model'] if state else None,
            'text_length': len(original['text'] if original else row['text']),
        }
