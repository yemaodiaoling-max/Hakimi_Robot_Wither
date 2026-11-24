"""MemoryStore maintenance: ingestion sync, consolidation, index rebuilds, pruning and daily backups."""
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from hakimi.core.runtime_io import read_json_cached
from hakimi.memory.lifecycle_rules import expanded_terms, explicit_person
from hakimi.memory.store_base import _summary, content_hash, index_text, vectorize
from hakimi.memory.text import terms


class StoreMaintenanceMixin:
    """Mixin of MemoryStore; see memory_store."""

    # ---------------------------------------------------------------- ingestion
    def backfill_chat_metadata(self, *, limit=5000, max_bytes=2 * 1024 * 1024):
        """Idempotently enrich already-ingested chat rows from retained JSONL metadata.

        This uses a separate bounded cursor, so it still runs when the ingestion
        cursor is at EOF.  It deliberately never touches text, ids, weights or
        graph evidence; pruned text is not re-imported.
        """
        path = self.base / "state/chat_history.jsonl"
        try:
            stat = path.stat()
        except FileNotFoundError:
            return 0
        name = "metadata:chat_history.jsonl"
        updated = 0
        identity = str(stat.st_ino)
        with self.db() as db:
            cursor = db.execute("SELECT identity,offset FROM cursors WHERE name=?", (name,)).fetchone()
            offset = cursor["offset"] if cursor and cursor["identity"] == identity and cursor["offset"] <= stat.st_size else 0
            if cursor and cursor["identity"] == identity and offset == stat.st_size:
                return 0
            with path.open("rb") as stream:
                stream.seek(offset)
                for _ in range(max(1, min(int(limit), 20000))):
                    if stream.tell() - offset >= max(65536, int(max_bytes)):
                        break
                    line = stream.readline()
                    if not line.endswith(b"\n"):
                        break
                    position = stream.tell()
                    try:
                        row = json.loads(line)
                    except (ValueError, TypeError, UnicodeError):
                        row = None
                    if isinstance(row, dict):
                        session = str(row.get("session_key", ""))
                        origin = str(row.get("message_id", ""))
                        nickname = str(row.get("nickname") or "")[:80]
                        if (re.fullmatch(r"(?:group|private):[1-9][0-9]{0,19}", session)
                                and origin and nickname):
                            changed = db.execute(
                                "UPDATE records SET nickname=? WHERE kind='chat' AND session=? "
                                "AND origin=? AND nickname=''", (nickname, session, origin),
                            ).rowcount
                            updated += max(0, int(changed or 0))
                    db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?)", (name, identity, position))
        return updated

    def sync(self):
        """Incremental, bounded import; replacement/trimming and partial rows are safe."""
        self.backfill_chat_metadata()
        for name in ("chat_history.jsonl", "session_memory.jsonl"):
            path = self.base / "state" / name
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            identity = str(stat.st_ino)
            with self.db() as db:
                cursor = db.execute("SELECT identity,offset FROM cursors WHERE name=?", (name,)).fetchone()
                offset = cursor["offset"] if cursor and cursor["identity"] == identity and cursor["offset"] <= stat.st_size else 0
                if cursor and offset == stat.st_size and cursor["identity"] == identity:
                    continue
                with path.open("rb") as stream:
                    stream.seek(offset)
                    for _ in range(5000):
                        if stream.tell() - offset >= 2 * 1024 * 1024:
                            break
                        line = stream.readline()
                        if not line.endswith(b"\n"):
                            break
                        position = stream.tell()
                        try:
                            row = json.loads(line)
                            if isinstance(row, dict):
                                self._put(db, row, "chat" if name.startswith("chat_") else row.get("kind", "preference"))
                        except (ValueError, TypeError):
                            pass
                        db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?)", (name, identity, position))
        try:
            from hakimi.memory.bili_autobiography import sync_bili_autobiography
            sync_bili_autobiography(self)
        except (OSError, sqlite3.Error, ValueError):
            pass
        self.consolidate()
        try:
            self.rebuild_graph(limit=64)
        except (sqlite3.Error, ValueError, TypeError):
            pass
        if time.monotonic() >= self.next_prune:
            self.prune()
            self.next_prune = time.monotonic() + 3600

    def consolidate(self, limit=256):
        """Incremental one-time backfill plus normal ingest; never extra model calls."""
        with self.db() as db:
            cursor = db.execute("SELECT value FROM memory_meta WHERE key='consolidated_id'").fetchone()
            after = int(cursor[0]) if cursor else 0
            rows = list(db.execute("SELECT * FROM records WHERE kind='chat' AND id>? ORDER BY id LIMIT ?", (after, limit)))
            for r in rows:
                if r['text']:
                    tokens = expanded_terms(index_text(r['text']) + ' ' + r['key'], lambda value: terms(value, limit=32000))
                    try:
                        self._insert_search(db, r['id'], r['text'], r['key'])
                    except sqlite3.Error:
                        pass
                    db.execute('UPDATE records SET vector=?,summary=?,content_hash=? WHERE id=?',
                               (vectorize(tokens), '', content_hash(r['text']), r['id']))
                row = {'session_key': r['session'], 'user_id': r['user_id'], 'text': r['text'],
                       'message_id': r['origin'], 'received_at': r['at']}
                note = explicit_person(row)
                if note:
                    self._put(db, note, 'person')
            if rows:
                db.execute("INSERT OR REPLACE INTO memory_meta VALUES ('consolidated_id',?)", (str(rows[-1]['id']),))
        return len(rows)

    def rebuild_search_index(self, limit=256):
        with self.db() as db:
            rows = db.execute(
                "SELECT r.id,r.text,r.summary,r.key FROM records r LEFT JOIN search s ON s.rowid=r.id "
                "WHERE s.rowid IS NULL AND r.state!='deleted' AND (r.text!='' OR r.summary!='') ORDER BY r.id LIMIT ?",
                (max(1, min(int(limit), 2000)),),
            ).fetchall()
            for row in rows:
                self._insert_search(db, row['id'], row['text'] or row['summary'], row['key'])
        return len(rows)

    def _active_social_evidence_ids(self, db):
        """Only evidence M ids that an active social projection still references.

        The pin is deliberately narrow: it never preserves a whole conversation,
        never resurrects a deleted/erased row, and is capped at the same eight
        ids the social writer accepts.  Private evidence stays private; it is
        pinned only for its own projection, not exposed to another scope.
        """
        pinned = set()
        try:
            rows = db.execute(
                "SELECT evidence_record_ids FROM social_assessments WHERE state='active'"
            ).fetchall()
        except sqlite3.Error:
            return pinned
        for row in rows:
            try:
                values = json.loads(row[0] or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(values, list):
                continue
            for value in values[:8]:
                try:
                    number = int(str(value).strip().upper().removeprefix("M"))
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    pinned.add(number)
        if not pinned:
            return set()
        marks = ",".join("?" for _ in pinned)
        try:
            live = db.execute(
                "SELECT id FROM records WHERE id IN (" + marks + ") AND state!='deleted' "
                "AND text!=''", sorted(pinned),
            ).fetchall()
        except sqlite3.Error:
            return set()
        return {int(row[0]) for row in live}

    DURABLE_KINDS = ("person", "preference", "lore", "episode", "reflection", "commitment")

    def migrate_v8_expiry(self, *, dry_run=False, now=None):
        """Owner-invoked, idempotent V8 expiry migration; never runs on import.

        Only the ``expires``/``expires_at`` projection columns change.  Original
        text, hashes, ids, versions, state, evidence and supersede links are
        untouched.  Rows already erased are left erased (not resurrected).
        """
        now = time.time() if now is None else float(now)
        kinds = ",".join("?" for _ in self.DURABLE_KINDS)
        with self.db() as db:
            durable = db.execute(
                "SELECT COUNT(*) FROM records WHERE state='active' AND expires!=0 "
                "AND kind IN (" + kinds + ")", self.DURABLE_KINDS,
            ).fetchone()[0]
            try:
                social = db.execute(
                    "SELECT COUNT(*) FROM social_assessments WHERE state='active' AND expires_at!=0"
                ).fetchone()[0]
            except sqlite3.Error:
                social = 0
            if not dry_run:
                db.execute(
                    "UPDATE records SET expires=0 WHERE state='active' AND expires!=0 "
                    "AND kind IN (" + kinds + ")", self.DURABLE_KINDS,
                )
                try:
                    db.execute(
                        "UPDATE social_assessments SET expires_at=0 "
                        "WHERE state='active' AND expires_at!=0"
                    )
                except sqlite3.Error:
                    pass
        return {"ok": True, "dry_run": bool(dry_run), "at": now,
                "durable_records_to_update": int(durable or 0),
                "social_assessments_to_update": int(social or 0)}

    def consolidate_person_impressions(self, now=None, *, max_chars=360):
        """Deduplicate orphaned active impressions and compact bloated persona texts."""
        now = time.time() if now is None else float(now)
        from hakimi.social.impression_forgetting import compact_impression_text

        superseded_records = 0
        superseded_assessments = 0
        compacted_assessments = 0

        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            # Step A: Deduplicate orphaned active 关系印象·* rows in records
            records_rows = db.execute(
                "SELECT id, user_id, session, at FROM records "
                "WHERE kind='person' AND state='active' AND key LIKE '关系印象·%' "
                "ORDER BY at DESC, id DESC"
            ).fetchall()

            seen_record_scopes = {}
            for row in records_rows:
                rid = int(row["id"])
                user_id = str(row["user_id"])
                session = str(row["session"] or "")
                scope = "groups" if session.startswith("group:") else session
                scope_key = (user_id, scope)
                if scope_key not in seen_record_scopes:
                    seen_record_scopes[scope_key] = rid
                else:
                    newest_id = seen_record_scopes[scope_key]
                    db.execute(
                        "UPDATE records SET state='superseded', superseded_by=? WHERE id=?",
                        (newest_id, rid)
                    )
                    superseded_records += 1

            # Step A (cont): Deduplicate active social_assessments per (target_id, scope, provenance)
            # Prefer real person notebooks (relationship_type!='relationship_event') over event-only placeholders.
            assessment_rows = db.execute(
                "SELECT id, target_id, session, provenance, relationship_type, created_at FROM social_assessments "
                "WHERE subject_id='hakimi' AND provenance IN ('gemini_manual', 'dsh_assisted') "
                "AND state='active' ORDER BY (relationship_type!='relationship_event') DESC, "
                "(provenance='gemini_manual') DESC, created_at DESC, id DESC"
            ).fetchall()

            seen_assessment_scopes = {}
            seen_notebook_scopes = {}
            for row in assessment_rows:
                aid = int(row["id"])
                target_id = str(row["target_id"])
                provenance = str(row["provenance"])
                rel_type = str(row["relationship_type"] or "")
                session = str(row["session"] or "")
                scope = "groups" if session.startswith("group:") else session
                if rel_type != "relationship_event" and (target_id, scope) not in seen_notebook_scopes:
                    seen_notebook_scopes[(target_id, scope)] = aid
                if rel_type == "relationship_event" and (target_id, scope) in seen_notebook_scopes:
                    db.execute(
                        "UPDATE social_assessments SET state='superseded', superseded_by=? WHERE id=?",
                        (seen_notebook_scopes[(target_id, scope)], aid)
                    )
                    superseded_assessments += 1
                    continue
                scope_key = (target_id, scope, provenance)
                if scope_key not in seen_assessment_scopes:
                    seen_assessment_scopes[scope_key] = aid
                else:
                    newest_id = seen_assessment_scopes[scope_key]
                    db.execute(
                        "UPDATE social_assessments SET state='superseded', superseded_by=? WHERE id=?",
                        (newest_id, aid)
                    )
                    superseded_assessments += 1

            # Step B: Compact long or stale impressions
            active_assessments = db.execute(
                "SELECT id, original_record_id, impression, created_at FROM social_assessments "
                "WHERE subject_id='hakimi' AND provenance IN ('gemini_manual', 'dsh_assisted') "
                "AND state='active' ORDER BY id ASC"
            ).fetchall()

            for row in active_assessments:
                aid = int(row["id"])
                orig_rid = int(row["original_record_id"]) if row["original_record_id"] else None
                impression = str(row["impression"] or "")
                created_at = float(row["created_at"] or 0)
                age_days = max(0.0, (now - created_at) / 86400.0)

                if len(impression) > max_chars or (age_days >= 5.0 and len(impression) > 240):
                    compacted = compact_impression_text(
                        impression, max_chars=max_chars, age_days=age_days
                    )
                    if compacted and compacted != impression:
                        db.execute(
                            "UPDATE social_assessments SET impression=? WHERE id=?",
                            (compacted, aid)
                        )
                        if orig_rid:
                            record_row = db.execute(
                                "SELECT id, key FROM records WHERE id=?", (orig_rid,)
                            ).fetchone()
                            if record_row:
                                summary = _summary(compacted)
                                db.execute(
                                    "UPDATE records SET text=?, summary=? WHERE id=?",
                                    (compacted, summary, orig_rid)
                                )
                                try:
                                    self._insert_search(db, orig_rid, compacted, record_row["key"])
                                except sqlite3.Error:
                                    pass
                        compacted_assessments += 1

        return {
            "superseded_records": superseded_records,
            "superseded_assessments": superseded_assessments,
            "compacted_assessments": compacted_assessments,
        }

    def prune(self, now=None):
        now = time.time() if now is None else now
        self.consolidate_person_impressions(now)
        policy = read_json_cached(self.base / 'config.json', {}, readonly=True).get('memory_policy', {})
        days = float(policy.get('recent_original_days', 7))
        limit = max(100, int(policy.get('recent_original_limit', 3000)))
        floor = max(0, min(200, int(policy.get('per_session_minimum', 200))))
        min_hours = max(0.0, float(policy.get('raw_min_retention_hours', 24)))
        self.backup_daily(now)
        with self.db() as db:
            # Evidence referenced by an active social projection outlives the
            # ordinary 7-day/3000-row raw-chat retention.  Everything else is
            # still trimmed, so this cannot become a second chat archive.
            pinned = self._active_social_evidence_ids(db)
            transient = [r[0] for r in db.execute("SELECT id FROM records WHERE kind='dialogue' AND expires<=?", (now,))]
            # A global row budget must not erase recent active conversations.
            # The ordinary budget applies separately to older retained originals;
            # retrieval and model-input bounds remain independent of retention.
            recent = {r[0] for r in db.execute("SELECT id FROM records WHERE kind='chat' AND state='active' AND text!='' AND at>=?", (now - min_hours * 3600,))}
            live = list(db.execute("SELECT id,at,row_number() OVER(PARTITION BY session ORDER BY at DESC,id DESC) rn FROM records WHERE kind='chat' AND text!='' AND at>=? AND NOT (state='active' AND at>=?)", (now - days * 86400, now - min_hours * 3600)))
            keep = {r['id'] for r in sorted(live, key=lambda r: (r['rn'] <= floor, r['at'], r['id']), reverse=True)[:limit]}
            keep.update(recent)
            keep.update(pinned)
            old = [r[0] for r in db.execute("SELECT id FROM records WHERE kind='chat' AND text!=''") if r[0] not in keep]
            from hakimi.memory.daily_events import protect_before_prune
            protected = protect_before_prune(db, old, now)
            old = [rid for rid in old if rid not in protected]
            for rid in transient:
                db.execute('DELETE FROM search WHERE rowid=?', (rid,))
                db.execute('DELETE FROM edges WHERE record_id=?', (rid,))
                db.execute('DELETE FROM embeddings WHERE record_id=?', (rid,))
                db.execute('DELETE FROM embedding_state WHERE record_id=?', (rid,))
                db.execute('DELETE FROM graph_edge_evidence WHERE record_id=?', (rid,))
                db.execute('DELETE FROM graph_aliases WHERE source_record_id=?', (rid,))
                db.execute('DELETE FROM records WHERE id=?', (rid,))
            for rid in old:
                db.execute("UPDATE records SET text='',summary='',vector=X'',content_hash='' WHERE id=?", (rid,))
                db.execute('DELETE FROM search WHERE rowid=?', (rid,))
                db.execute('DELETE FROM edges WHERE record_id=?', (rid,))
                db.execute('DELETE FROM embeddings WHERE record_id=?', (rid,))
                db.execute('DELETE FROM embedding_state WHERE record_id=?', (rid,))
                # Raw chat words have expired with the record.  Keep the evidence
                # row/date as provenance, but never the deleted excerpt or alias.
                db.execute("UPDATE graph_edge_evidence SET excerpt='' WHERE record_id=?", (rid,))
                db.execute('DELETE FROM graph_aliases WHERE source_record_id=?', (rid,))
            # Also clean rows pruned by an older version: their text is already
            # gone, but the excerpt/alias must not survive indefinitely.
            db.execute("UPDATE graph_edge_evidence SET excerpt='' WHERE record_id IN "
                       "(SELECT id FROM records WHERE kind='chat' AND text='')")
            db.execute("DELETE FROM graph_aliases WHERE source_record_id IN "
                       "(SELECT id FROM records WHERE kind='chat' AND text='')")
            # Manual originals survive every cleanup and never enter the archive path.
            for r in db.execute("SELECT id FROM (SELECT id,row_number() OVER(PARTITION BY session ORDER BY at DESC,id DESC) rn FROM records WHERE kind='episode' AND state='active' AND original_id IS NULL AND channel NOT IN ('daily_events', 'bilibili_watch')) WHERE rn>40").fetchall():
                db.execute("UPDATE records SET state='archived' WHERE id=?", (r[0],))
            # Bilibili autobiography entries expire by importance tier and enforce a hard count cap.
            bili_limit = max(50, int(policy.get('bilibili_watch_limit', 240)))
            bili_expired = [r[0] for r in db.execute(
                "SELECT id FROM records WHERE channel='bilibili_watch' AND ((expires>0 AND expires<=?) OR state!='active')",
                (now,),
            ).fetchall()]
            bili_overflow = [r[0] for r in db.execute(
                "SELECT id FROM records WHERE channel='bilibili_watch' AND state='active' AND (expires=0 OR expires>?) "
                "ORDER BY (expires=0 OR (expires-at)>2592000) DESC, at DESC, id DESC LIMIT -1 OFFSET ?",
                (now, bili_limit),
            ).fetchall()]
            if bili_expired or bili_overflow:
                from hakimi.memory.bili_autobiography import purge_record_ids
                purge_record_ids(db, [*bili_expired, *bili_overflow])
            # Small valuable daily cards survive raw expiry and remain searchable;
            # their input/batch limits prevent creating a second raw-chat archive.
            old_shells = [r[0] for r in db.execute(
                "SELECT id FROM records WHERE kind='chat' AND text='' AND at<?", (now - 180 * 86400,))
                if r[0] not in pinned]
            for rid in old_shells:
                db.execute("DELETE FROM embeddings WHERE record_id=?", (rid,))
                db.execute("DELETE FROM embedding_state WHERE record_id=?", (rid,))
                db.execute("DELETE FROM graph_edge_evidence WHERE record_id=?", (rid,))
                db.execute("DELETE FROM graph_aliases WHERE source_record_id=?", (rid,))
                db.execute("DELETE FROM records WHERE id=?", (rid,))
            db.execute("DELETE FROM graph_edges WHERE edge_id NOT IN (SELECT DISTINCT edge_id FROM graph_edge_evidence)")
        return len(old)

    def backup_daily(self, now=None):
        """Consistent SQLite snapshot; only the newest daily file is kept. No new daemon."""
        from datetime import datetime, timezone
        now = time.time() if now is None else now
        folder = self.base / 'state/memory_backups'
        folder.mkdir(exist_ok=True)
        target = folder / (datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d') + '.sqlite3')
        if target.exists():
            return
        temporary = target.with_suffix('.' + str(os.getpid()) + '.tmp')
        try:
            source = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
            try:
                with closing(sqlite3.connect(temporary)) as destination:
                    source.backup(destination)
            finally:
                source.close()
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        for old in sorted(folder.glob('????-??-??.sqlite3'))[:-1]:
            old.unlink()
