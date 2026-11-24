import sqlite3
import unittest
from hakimi.memory.store_retrieval import StoreRetrievalMixin
from hakimi.social.social_fields import _find_prior_impression


class SharedPrivateMemoryTests(unittest.TestCase):
    def test_group_reads_present_person_private_memory_with_sources_intact(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE records(id INTEGER,session TEXT,user_id TEXT,kind TEXT,state TEXT,shared INTEGER,text TEXT)')
            db.executemany('INSERT INTO records VALUES(?,?,?,?,?,?,?)', [
                (1,'group:123','42','chat','active',0,'群聊原文'),
                (2,'private:42','42','chat','active',0,'群友9d04的私聊原文'),
                (3,'private:42','','commitment','active',0,'约定原文'),
                (4,'private:99','99','chat','active',0,'别人的私聊'),
                (5,'private:42','42','chat','deleted',0,'已删除'),
            ])
            for cross in [False, True]:
                sql, args = StoreRetrievalMixin.scope('group:123', ['42'], cross)
                rows = db.execute('SELECT id,session,text FROM records r WHERE '+sql+' ORDER BY id', args).fetchall()
                self.assertEqual([r[0] for r in rows], [1,2,3])
                self.assertEqual(rows[1][1:], ('private:42','群友9d04的私聊原文'))

    def test_private_peer_is_linked_even_without_visible_user_list(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE records(id INTEGER,session TEXT,user_id TEXT,kind TEXT,state TEXT,shared INTEGER)')
            db.executemany('INSERT INTO records VALUES(?,?,?,?,?,?)', [
                (1,'private:42','42','chat','active',0),
                (2,'group:123','42','chat','active',0),
                (3,'group:456','42','person','active',1),
                (4,'private:99','99','chat','active',0),
            ])
            sql, args = StoreRetrievalMixin.scope('private:42', [])
            self.assertEqual([r[0] for r in db.execute('SELECT id FROM records r WHERE '+sql+' ORDER BY id', args)], [1,2,3])

    def test_person_notebook_append_uses_latest_shared_impression(self):
        with sqlite3.connect(':memory:') as db:
            db.row_factory = sqlite3.Row
            db.execute('CREATE TABLE social_assessments(id INTEGER,session TEXT,subject_id TEXT,target_id TEXT,provenance TEXT,state TEXT,created_at REAL,impression TEXT)')
            db.executemany('INSERT INTO social_assessments VALUES(?,?,?,?,?,?,?,?)', [
                (1,'group:123','hakimi','42','gemini_manual','active',1,'群聊印象'),
                (2,'private:42','hakimi','42','gemini_manual','active',2,'私聊新印象'),
                (3,'private:99','hakimi','42','gemini_manual','active',3,'其他私聊'),
            ])
            for session in ['group:123','private:42']:
                row = _find_prior_impression(db, dict(session=session,subject_id='hakimi',target_id='42',provenance='gemini_manual'))
                self.assertEqual(row['id'], 2)
                self.assertEqual(row['impression'], '私聊新印象')
