"""Offline tests for the read-only Hakimi social behaviour projection.

No model, network, QQ, or deployment path is touched.  The fixture creates a
small temporary SQLite database directly so the tests can prove that lookups do
not initialise, migrate, or write it.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hakimi.social import memory_social
from hakimi.social import social_behavior
from hakimi.memory.memory_store import MemoryStore
from _repo import repo_file


RECORDS_SCHEMA = """
CREATE TABLE records (
 id INTEGER PRIMARY KEY,
 session TEXT NOT NULL DEFAULT '',
 user_id TEXT NOT NULL DEFAULT '',
 kind TEXT NOT NULL DEFAULT 'chat',
 at REAL NOT NULL DEFAULT 0,
 expires REAL NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'active',
 text TEXT NOT NULL DEFAULT '',
 direct_targets TEXT NOT NULL DEFAULT '',
 nickname TEXT NOT NULL DEFAULT ''
);
"""


class SocialBehaviorV5Tests(unittest.TestCase):
    NOW = 1_900_000_000.0

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        (self.base / "state").mkdir()
        self.db_path = self.base / "state" / "memory.sqlite3"
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(RECORDS_SCHEMA)
        self.db.executescript(memory_social.SCHEMA)
        self._request = 0
        self.db.commit()

    def tearDown(self):
        try:
            sqlite3.connect(self.db_path).close()
        except sqlite3.Error:
            pass
        try:
            self.db.close()
        except sqlite3.Error:
            pass
        self.tempdir.cleanup()

    # ------------------------------------------------------------------ fixture
    def clear_rows(self):
        self.db.execute("DELETE FROM social_assessments")
        self.db.execute("DELETE FROM records")
        self.db.commit()
        self._request = 0

    def add_evidence(self, *, session="group:1", user="101", text="公开聊天证据",
                     state="active", kind="chat", at=None, expires=0,
                     direct_targets=""):
        cursor = self.db.execute(
            "INSERT INTO records(session,user_id,kind,at,expires,state,text,direct_targets) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (session, str(user), kind, self.NOW - 100 if at is None else at,
             expires, state, text, direct_targets),
        )
        return int(cursor.lastrowid)

    def add_assessment(self, *, target="101", subject="hakimi", affinity=5,
                       confidence=80, created_at=None, session="group:1",
                       provider="gemini", provenance="gemini_manual",
                       author="gemini", evidence=(), original_record_id=None,
                       seed_id="", state="active"):
        self._request += 1
        if created_at is None:
            created_at = self.NOW - 3600
        if original_record_id is None and evidence:
            original_record_id = evidence[0]
        cursor = self.db.execute(
            "INSERT INTO social_assessments("
            "request_id,subject_id,target_id,author,provider,provenance,impression,"
            "affinity,confidence,evidence_record_ids,original_record_id,created_at,"
            "session,state,seed_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("req-" + str(self._request), str(subject), str(target), author, provider,
             provenance, "测试印象", affinity, confidence,
             json.dumps(list(evidence)), original_record_id, created_at,
             session, state, seed_id),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def add_pair(self, *, person="101", latest_age=2 * 3600,
                 gap=10 * 3600, older_session="group:1", latest_session="group:1",
                 older_evidence_session="group:1", latest_evidence_session="group:1",
                 older_evidence_state="active", latest_evidence_state="active",
                 older_text="较早的负面证据", latest_text="较新的负面证据",
                 older_affinity=3, latest_affinity=7,
                 older_confidence=80, latest_confidence=85,
                 older_provider="gemini", latest_provider="gemini",
                 older_provenance="gemini_manual", latest_provenance="gemini_manual",
                 older_author="gemini", latest_author="gemini",
                 older_subject="hakimi", latest_subject="hakimi",
                 older_original=None, latest_original=None,
                 older_state="active", latest_state="active"):
        older_created = self.NOW - latest_age - gap
        latest_created = self.NOW - latest_age
        older_evidence = self.add_evidence(
            session=older_evidence_session, user=person, text=older_text,
            state=older_evidence_state, at=older_created - 120,
        )
        latest_evidence = self.add_evidence(
            session=latest_evidence_session, user=person, text=latest_text,
            state=latest_evidence_state, at=latest_created - 120,
        )
        older = self.add_assessment(
            target=person, subject=older_subject, affinity=older_affinity,
            confidence=older_confidence, created_at=older_created,
            session=older_session, provider=older_provider,
            provenance=older_provenance, author=older_author,
            evidence=[older_evidence],
            original_record_id=(older_evidence if older_original is None else older_original),
            state=older_state,
        )
        latest = self.add_assessment(
            target=person, subject=latest_subject, affinity=latest_affinity,
            confidence=latest_confidence, created_at=latest_created,
            session=latest_session, provider=latest_provider,
            provenance=latest_provenance, author=latest_author,
            evidence=[latest_evidence],
            original_record_id=(latest_evidence if latest_original is None else latest_original),
            state=latest_state,
        )
        return older_evidence, latest_evidence, older, latest

    def profile(self, person="101", session="group:1", config=None):
        return social_behavior.behavior_for(
            self.base, person, session, now=self.NOW, config=config,
        )

    def rejected(self, person="101", session="group:1", config=None):
        return social_behavior.should_reject_at(
            self.base, session, person, now=self.NOW, config=config,
        )

    # ------------------------------------------------------------------ profile
    def test_warm_profile_is_json_safe_and_uses_1_15(self):
        evidence = self.add_evidence()
        self.add_assessment(affinity=80, confidence=88, evidence=[evidence])
        profile = self.profile()
        self.assertEqual(profile["band"], "warm")
        self.assertAlmostEqual(profile["initiative_multiplier"], 1.15)
        self.assertEqual(profile["affinity"], 80)
        self.assertEqual(profile["confidence"], 88)
        self.assertTrue(profile["known"])
        self.assertEqual(profile["assessment_id"], 1)
        self.assertEqual(profile["evidence_memory_ids"], ["M" + str(evidence)])
        self.assertIn("语气", profile["guidance"])
        json.dumps(profile)  # must be JSON-safe

    def test_profile_bands_and_multipliers(self):
        cases = (
            (75, "warm", 1.15),
            (74, "neutral", 1.0),
            (35, "neutral", 1.0),
            (34, "reserved", 0.55),
            (11, "reserved", 0.55),
            (10, "distant", 0.2),
            (0, "distant", 0.2),
        )
        for index, (affinity, band, multiplier) in enumerate(cases, start=101):
            evidence = self.add_evidence(user=str(index))
            self.add_assessment(
                target=str(index), affinity=affinity, confidence=80,
                evidence=[evidence], created_at=self.NOW - index,
            )
            profile = self.profile(person=str(index))
            self.assertEqual(profile["band"], band, affinity)
            self.assertAlmostEqual(profile["initiative_multiplier"], multiplier)

    def test_unknown_without_social_table_and_never_invents_fifty(self):
        self.db.execute("DROP TABLE social_assessments")
        self.db.commit()
        profile = self.profile()
        self.assertEqual(profile["band"], "unknown")
        self.assertIsNone(profile["affinity"])
        self.assertIsNone(profile["confidence"])
        self.assertAlmostEqual(profile["initiative_multiplier"], 1.0)
        self.assertFalse(profile["known"])
        self.assertNotIn(50, [profile["affinity"], profile["confidence"]])
        self.assertFalse(self.rejected())

    def test_missing_database_is_neutral_and_not_created(self):
        missing = self.base / "missing"
        profile = social_behavior.behavior_for(missing, "101", "group:1", now=self.NOW)
        self.assertEqual(profile["band"], "unknown")
        self.assertIsNone(profile["affinity"])
        self.assertFalse(social_behavior.should_reject_at(
            missing, "group:1", "101", now=self.NOW,
        ))
        self.assertFalse((missing / "state" / "memory.sqlite3").exists())

    def test_low_confidence_is_unknown_neutral(self):
        evidence = self.add_evidence()
        self.add_assessment(affinity=95, confidence=59, evidence=[evidence])
        profile = self.profile()
        self.assertEqual(profile["band"], "unknown")
        self.assertAlmostEqual(profile["initiative_multiplier"], 1.0)
        self.assertFalse(profile["known"])
        self.assertIsNone(profile["trusted_affinity"])

    def test_expired_evidence_downgrades_profile_to_unknown(self):
        evidence = self.add_evidence(state="deleted")
        self.add_assessment(affinity=90, confidence=90, evidence=[evidence])
        profile = self.profile()
        self.assertEqual(profile["band"], "unknown")
        self.assertAlmostEqual(profile["initiative_multiplier"], 1.0)
        self.assertEqual(profile["reason"], "evidence_missing_or_expired")

    def test_group_cross_group_and_private_exact_scope(self):
        group_evidence = self.add_evidence(session="group:3", user="101")
        self.add_assessment(
            affinity=80, confidence=80, session="group:2",
            evidence=[group_evidence], original_record_id=group_evidence,
        )
        self.assertEqual(self.profile(session="group:1")["band"], "warm")

        private_evidence = self.add_evidence(session="private:101", user="101")
        self.add_assessment(
            target="101", affinity=20, confidence=80, session="private:101",
            evidence=[private_evidence], created_at=self.NOW - 100,
        )
        # A private chat uses only that exact private assessment and evidence.
        self.assertEqual(self.profile(session="private:101")["band"], "reserved")
        self.assertEqual(self.profile(session="private:202")["band"], "unknown")
        self.assertEqual(self.profile(session="group:1")["band"], "warm")

        # A private assessment cannot borrow public-group evidence.
        self.add_assessment(
            target="101", affinity=90, confidence=90, session="private:202",
            evidence=[private_evidence], created_at=self.NOW - 50,
        )
        self.assertEqual(self.profile(session="private:202")["band"], "unknown")

    def test_dsh_terra_other_member_and_other_subject_are_excluded(self):
        evidence = self.add_evidence()
        # dsh and terra providers are never Hakimi's own manual view.
        self.add_assessment(affinity=0, confidence=99, provider="dsh",
                            provenance="dsh_assisted", evidence=[evidence],
                            created_at=self.NOW - 5)
        self.add_assessment(affinity=0, confidence=99, provider="terra",
                            provenance="gemini_manual", evidence=[evidence],
                            created_at=self.NOW - 4)
        # A Terra-authored row with a misleading Gemini provider is also out.
        self.add_assessment(affinity=0, confidence=99, author="terra",
                            evidence=[evidence], created_at=self.NOW - 3)
        # Another member's opinion about 101 is not Hakimi's opinion.
        self.add_assessment(subject="202", target="101", affinity=0,
                            confidence=99, evidence=[evidence],
                            created_at=self.NOW - 2)
        profile = self.profile()
        self.assertEqual(profile["band"], "unknown")
        self.assertIsNone(profile["affinity"])
        self.assertFalse(self.rejected())

    # ------------------------------------------------------------------ @ gate
    def test_single_negative_judgment_never_gates(self):
        evidence = self.add_evidence()
        self.add_assessment(affinity=2, confidence=90, evidence=[evidence],
                            created_at=self.NOW - 20 * 3600)
        self.assertFalse(self.rejected())
        self.add_assessment(affinity=2, confidence=90, evidence=[evidence],
                            created_at=self.NOW - 2 * 3600)
        self.assertFalse(self.rejected())

    def test_two_independent_valid_negatives_gate(self):
        self.add_pair()
        self.assertTrue(self.rejected())
        self.assertTrue(self.profile()["at_gate"])

    def test_latest_two_must_be_at_least_six_hours_apart(self):
        self.add_pair(gap=5 * 3600)
        self.assertFalse(self.rejected())
        self.add_pair(gap=6 * 3600, person="102")
        self.assertTrue(self.rejected(person="102"))

    def test_latest_judgment_must_be_within_24_hours(self):
        self.add_pair(latest_age=25 * 3600)
        self.assertFalse(self.rejected())
        self.add_pair(latest_age=24 * 3600, person="102")
        self.assertTrue(self.rejected(person="102"))

    def test_later_rebound_or_unknown_judgment_cancels_gate(self):
        self.add_pair()
        self.assertTrue(self.rejected())
        rebound_evidence = self.add_evidence(user="101", text="后来关系缓和")
        self.add_assessment(affinity=80, confidence=85, evidence=[rebound_evidence],
                            created_at=self.NOW - 600)
        self.assertFalse(self.rejected())
        self.assertEqual(self.profile()["band"], "warm")

        # An unknown newer judgment also cancels; it does not fall back to the
        # older negative pair.
        unknown_evidence = self.add_evidence(user="101", text="材料不足")
        self.add_assessment(affinity=5, confidence=40, evidence=[unknown_evidence],
                            created_at=self.NOW - 300)
        self.assertFalse(self.rejected())
        self.assertEqual(self.profile()["band"], "unknown")

    def test_duplicate_replay_is_not_two_independent_judgments(self):
        evidence = self.add_evidence()
        self.add_assessment(affinity=2, confidence=90, evidence=[evidence],
                            original_record_id=evidence,
                            created_at=self.NOW - 12 * 3600)
        self.add_assessment(affinity=2, confidence=90, evidence=[evidence],
                            original_record_id=evidence,
                            created_at=self.NOW - 2 * 3600)
        self.assertFalse(self.rejected())

    def test_expired_deleted_and_pruned_evidence_cancels_gate(self):
        self.add_pair(latest_evidence_state="deleted")
        self.assertFalse(self.rejected())

        # A fresh row set for the pruned older evidence case.
        self.clear_rows()
        self.add_pair(older_text="")
        self.assertFalse(self.rejected())

        # A newer positive whose evidence was pruned must not reopen the older
        # negative pair either.
        self.clear_rows()
        self.add_pair()
        positive_evidence = self.add_evidence(text="")
        self.add_assessment(affinity=90, confidence=90, evidence=[positive_evidence],
                            created_at=self.NOW - 60)
        self.assertFalse(self.rejected())

    def test_group_only_and_private_negatives_never_gate(self):
        self.add_pair(older_session="private:101",
                      latest_session="private:101",
                      older_evidence_session="private:101",
                      latest_evidence_session="private:101")
        self.assertFalse(self.rejected(session="private:101"))
        self.assertFalse(self.rejected(session="group:1"))
        # Even a group gate elsewhere cannot be reused for a private chat.
        self.add_pair()
        self.assertTrue(self.rejected(session="group:1"))
        self.assertFalse(self.rejected(session="private:101"))

    def test_terra_and_other_subject_judgments_never_form_the_pair(self):
        # One genuine Gemini negative older, one Terra negative newer.
        older_evidence = self.add_evidence()
        self.add_assessment(affinity=2, confidence=90, evidence=[older_evidence],
                            created_at=self.NOW - 20 * 3600)
        terra_evidence = self.add_evidence()
        self.add_assessment(affinity=1, confidence=99, provider="terra",
                            provenance="gemini_manual", evidence=[terra_evidence],
                            created_at=self.NOW - 2 * 3600)
        self.assertFalse(self.rejected())

        # One genuine Gemini negative older, one other-member judgment newer.
        self.clear_rows()
        older_evidence = self.add_evidence()
        self.add_assessment(affinity=2, confidence=90, evidence=[older_evidence],
                            created_at=self.NOW - 20 * 3600)
        other_evidence = self.add_evidence()
        self.add_assessment(subject="202", target="101", affinity=1,
                            confidence=99, evidence=[other_evidence],
                            created_at=self.NOW - 2 * 3600)
        self.assertFalse(self.rejected())

    def test_superseded_negative_history_corroborates_active_negative(self):
        self.add_pair(older_state="superseded", latest_state="active")
        self.assertTrue(self.rejected())
        profile = self.profile()
        self.assertEqual(profile["band"], "distant")
        self.assertTrue(profile["at_gate"])

    def test_same_evidence_set_reused_later_never_gates(self):
        # The evidence is observed after the older judgment, so only the
        # "newer judgment adds an evidence id" rule can reject this reuse.
        evidence = self.add_evidence(at=self.NOW - 6 * 3600)
        self.add_assessment(
            affinity=2, confidence=90, evidence=[evidence],
            original_record_id=evidence, created_at=self.NOW - 8 * 3600,
            state="superseded",
        )
        self.add_assessment(
            affinity=3, confidence=91, evidence=[evidence],
            original_record_id=817740, created_at=self.NOW - 2 * 3600,
            state="active",
        )
        self.assertFalse(self.rejected())

    def test_expired_evidence_and_assessment_do_not_gate(self):
        self.add_pair()
        latest_evidence = self.db.execute(
            "SELECT id FROM records ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.db.execute("UPDATE records SET expires=? WHERE id=?",
                        (self.NOW - 1, latest_evidence))
        self.db.commit()
        self.assertFalse(self.rejected())
        self.assertEqual(self.profile()["reason"], "evidence_missing_or_expired")

        self.clear_rows()
        self.add_pair()
        if 'expires_at' not in {row[1] for row in self.db.execute('PRAGMA table_info(social_assessments)')}:
            self.db.execute("ALTER TABLE social_assessments ADD COLUMN expires_at REAL")
        latest_assessment = self.db.execute(
            "SELECT id FROM social_assessments ORDER BY created_at DESC,id DESC LIMIT 1"
        ).fetchone()[0]
        self.db.execute("UPDATE social_assessments SET expires_at=? WHERE id=?",
                        (self.NOW - 1, latest_assessment))
        self.db.commit()
        self.assertFalse(self.rejected())
        self.assertEqual(self.profile()["reason"], "assessment_expired")

        # An expired older history row cannot be used as corroboration either.
        self.clear_rows()
        self.add_pair()
        older_assessment = self.db.execute(
            "SELECT id FROM social_assessments ORDER BY created_at ASC,id ASC LIMIT 1"
        ).fetchone()[0]
        self.db.execute("UPDATE social_assessments SET expires_at=? WHERE id=?",
                        (self.NOW - 1, older_assessment))
        self.db.commit()
        self.assertFalse(self.rejected())

    def test_evidence_must_concern_target_and_survive_retraction(self):
        unrelated = self.add_evidence(
            user="202", text="101 只是被顺口提到", at=self.NOW - 200)
        self.add_assessment(affinity=90, confidence=90, evidence=[unrelated],
                            created_at=self.NOW - 100)
        profile = self.profile()
        self.assertEqual(profile["band"], "unknown")
        self.assertEqual(profile["reason"], "evidence_missing_or_expired")

        self.clear_rows()
        addressed = self.add_evidence(
            user="202", text="[CQ:at,qq=101] 在说你", at=self.NOW - 200,
            direct_targets='["101"]')
        self.add_assessment(affinity=90, confidence=90, evidence=[addressed],
                            created_at=self.NOW - 100)
        self.assertEqual(self.profile()["band"], "warm")

        self.clear_rows()
        self.add_pair(latest_evidence_state="retracted")
        self.assertFalse(self.rejected())

    def test_policy_file_owner_bypasses_gate_even_with_config_passed(self):
        # The package ships a template; the owner lives in the instance policy.
        from unittest.mock import patch as _patch
        from hakimi import paths as _paths
        from _repo import INSTANCE
        self.enterContext(_patch.object(_paths, "DEFAULTS_DIR", INSTANCE))
        bundled = json.loads(
            (repo_file("social_behavior_policy.json")).read_text(encoding="utf-8"))
        self.assertIn("8112866014149232024", bundled["protected_user_ids"])
        self.add_pair(person="8112866014149232024")
        self.assertFalse(self.rejected(
            person="8112866014149232024", config={"owner_ids": ["101"]}))
        self.assertTrue(self.profile(
            person="8112866014149232024",
            config={"owner_ids": ["101"]})["protected"])

        # Runtime policy is merged too, even when config is supplied directly.
        self.clear_rows()
        (self.base / "social_behavior_policy.json").write_text(
            json.dumps({"protected_user_ids": ["101"]}), encoding="utf-8")
        self.add_pair(person="101")
        config = {"bot_profile": {"owner_ids": ["999"]}}
        self.assertFalse(self.rejected(person="101", config=config))
        self.assertTrue(self.profile(person="101", config=config)["protected"])

    def test_configured_owner_and_creator_are_exempt_but_nickname_is_not(self):
        config = {
            "bot_profile": {
                "owner_ids": ["101"],
                "creator_ids": [102],
                "owner_nicknames": ["Alice"],
            },
            "interaction_policy": {"operator_ids": ["103"]},
            "user_bias": {"users": {"104": {"role": "owner"}}},
        }
        for index, person in enumerate(("101", "102", "103", "104"), start=1):
            self.add_pair(person=person, latest_age=index * 60)
        for person in ("101", "102", "103", "104"):
            self.assertFalse(self.rejected(person=person, config=config), person)
        # No nickname-based exemption: 105's card/nickname is irrelevant.
        self.add_pair(person="105", latest_age=300)
        self.assertTrue(self.rejected(person="105", config=config))

    def test_rejection_does_not_mutate_message_or_assessment_rows(self):
        self.add_pair()
        before_records = self.db.execute("SELECT count(*) FROM records").fetchone()[0]
        before_assessments = self.db.execute(
            "SELECT count(*) FROM social_assessments").fetchone()[0]
        before_text = self.db.execute(
            "SELECT text FROM records ORDER BY id LIMIT 1").fetchone()[0]
        self.assertTrue(self.rejected())
        self.assertTrue(self.rejected())
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0],
                         before_records)
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM social_assessments").fetchone()[0],
            before_assessments)
        self.assertEqual(self.db.execute(
            "SELECT text FROM records ORDER BY id LIMIT 1").fetchone()[0],
            before_text)

    # -------------------------------------------------------------- read-only
    def test_lookup_does_not_create_schema_or_change_rows(self):
        self.db.execute("DROP TABLE social_assessments")
        self.db.commit()
        before = self.db.execute(
            "SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
        before = [tuple(row) for row in before]
        profile = self.profile()
        self.assertFalse(self.rejected())
        after = [tuple(row) for row in self.db.execute(
            "SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()]
        self.assertEqual(before, after)
        self.assertEqual(profile["band"], "unknown")

        # With the real social table present, the file and rows stay byte-stable.
        self.db.executescript(memory_social.SCHEMA)
        self.db.commit()
        self.add_pair()
        self.db.commit()
        digest_before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        rows_before = [tuple(row) for row in self.db.execute(
            "SELECT id,state,affinity,confidence,evidence_record_ids,session "
            "FROM social_assessments ORDER BY id").fetchall()]
        self.assertTrue(self.rejected())
        self.profile()
        self.db.commit()
        digest_after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        rows_after = [tuple(row) for row in self.db.execute(
            "SELECT id,state,affinity,confidence,evidence_record_ids,session "
            "FROM social_assessments ORDER BY id").fetchall()]
        self.assertEqual(digest_before, digest_after)
        self.assertEqual(rows_before, rows_after)

    def test_read_only_file_is_queryable(self):
        self.add_pair()
        self.db.commit()
        try:
            os.chmod(self.db_path, stat.S_IREAD)
            self.assertTrue(self.rejected())
            self.assertEqual(self.profile()["band"], "distant")
        finally:
            os.chmod(self.db_path, stat.S_IREAD | stat.S_IWRITE)

    def test_concurrent_reads_are_stable(self):
        self.add_pair()
        evidence = self.add_evidence()
        self.add_assessment(affinity=80, confidence=80, evidence=[evidence],
                            created_at=self.NOW - 10)

        def read(index):
            if index % 2:
                return social_behavior.should_reject_at(
                    self.base, "group:1", "101", now=self.NOW,
                )
            return social_behavior.behavior_for(
                self.base, "101", "group:1", now=self.NOW,
            )["band"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(read, range(48)))
        for index, result in enumerate(results):
            if index % 2:
                self.assertFalse(result)
            else:
                self.assertEqual(result, "warm")


class SocialRecordIntegrationTests(unittest.TestCase):
    """Exercise the real memory_social writer instead of hand-built rows."""

    NOW = 1_900_000_000.0

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name).resolve()
        (self.base / "state").mkdir()
        self.store = MemoryStore(self.base)

    def tearDown(self):
        self.tempdir.cleanup()

    def observe(self, text, *, user="101", session="group:1", mid, at):
        self.store.observe({
            "session_key": session, "user_id": user, "message_id": mid,
            "received_at": at, "text": text,
        })
        with self.store.db() as db:
            return int(db.execute(
                "SELECT id FROM records WHERE origin=? AND session=? "
                "ORDER BY id DESC LIMIT 1", (str(mid), session)).fetchone()[0])

    def test_record_social_version_chain_supersedes_old_and_gates_on_fresh_chat(self):
        old_evidence = self.observe(
            "较早的负面聊天", user="101", mid="old", at=self.NOW - 8 * 3600)
        first = memory_social.record_social(
            self.base, "group:1", "旧印象：只是普通搭话", "101",
            relationship_type="群友", affinity=4, confidence=85,
            evidence_memory_ids=["M" + str(old_evidence)],
            origin_message_id="turn-old", now=self.NOW - 6 * 3600,
        )
        fresh_evidence = self.observe(
            "六小时后的新负面聊天", user="101", mid="new",
            at=self.NOW - 2 * 3600)
        second = memory_social.record_social(
            self.base, "group:1", "新印象：仍然明显疏远", "101",
            relationship_type="群友", affinity=6, confidence=88,
            evidence_memory_ids=["M" + str(fresh_evidence)],
            origin_message_id="turn-new", now=self.NOW,
        )
        self.assertEqual(second["version"], 2)
        with self.store.db() as db:
            rows = db.execute(
                "SELECT id,state,superseded_by FROM social_assessments ORDER BY id"
            ).fetchall()
        self.assertEqual([(row["state"], row["superseded_by"]) for row in rows],
                         [("superseded", second["assessment_id"]), ("active", None)])
        self.assertTrue(social_behavior.should_reject_at(
            self.base, "group:1", "101", now=self.NOW))
        profile = social_behavior.behavior_for(
            self.base, "101", "group:1", now=self.NOW)
        self.assertEqual(profile["assessment_id"], second["assessment_id"])
        self.assertEqual(profile["band"], "distant")
        self.assertTrue(profile["at_gate"])

    def test_record_social_same_evidence_revision_does_not_gate(self):
        evidence = self.observe(
            "同一段聊天证据", user="101", mid="same", at=self.NOW - 8 * 3600)
        memory_social.record_social(
            self.base, "group:1", "旧印象：普通群友", "101",
            relationship_type="群友", affinity=4, confidence=85,
            evidence_memory_ids=["M" + str(evidence)],
            origin_message_id="same-old", now=self.NOW - 6 * 3600,
        )
        memory_social.record_social(
            self.base, "group:1", "新印象：仍是负面，但没有新证据", "101",
            relationship_type="群友", affinity=6, confidence=88,
            evidence_memory_ids=["M" + str(evidence)],
            origin_message_id="same-new", now=self.NOW,
        )
        with self.store.db() as db:
            states = [row[0] for row in db.execute(
                "SELECT state FROM social_assessments ORDER BY id")]
        self.assertEqual(states, ["superseded", "active"])
        self.assertFalse(social_behavior.should_reject_at(
            self.base, "group:1", "101", now=self.NOW))
        self.assertEqual(social_behavior.behavior_for(
            self.base, "101", "group:1", now=self.NOW)["band"], "distant")


if __name__ == "__main__":
    unittest.main(verbosity=2)
