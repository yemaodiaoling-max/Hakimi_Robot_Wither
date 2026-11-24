import json
import tempfile
import unittest
from pathlib import Path
from bili_companion.runtime.storage import Database
from bili_companion.runtime.watcher import WatchState
from bili_companion.runtime.retention import prune_activity,RETENTION_SECONDS,retained_observation_count
from bili_companion.policy.video_comments import VideoCommentExecutor
from bili_companion.policy.budget import BudgetLedger


class RetentionTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'state'/'test_tmp'
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.database=Database(Path(self.temp.name)/'watch.sqlite3')
        self.state=WatchState(self.database,'8729095131')
        VideoCommentExecutor(self.database,None)
        self.now=1800000000
        self.cutoff=self.now-RETENTION_SECONDS

    def seed_video(self,bvid,at):
        with self.database.connect(write=True) as db:
            db.execute("INSERT INTO video_history VALUES (?,?,?,'reason',4,'high','continue','summary','[[0,30]]','[\"e\"]','sampled')",(bvid,at,at))
            db.execute('INSERT INTO video_selection_hints VALUES (?,\'hearthstone\',\'popular\')',(bvid,))

    def test_boundary_current_bookmark_and_permanent_tombstones(self):
        for bvid,at in [('old',self.cutoff-1),('edge',self.cutoff),('current',0),('bookmark',0)]:self.seed_video(bvid,at)
        self.state.change(lambda s:s.update(video={'bvid':'current'},bookmark={'bvid':'bookmark'}))
        with self.database.connect(write=True) as db:
            for state in ['sent','unknown','dispatching']:
                db.execute('INSERT INTO video_comment_attempts VALUES (?,?,?, ?,?,?,?,?)',('account',state,'session','comment',0,'day',state,'up'))
        self.assertEqual(prune_activity(self.database,self.now,scope='watcher')['state'],'pruned')
        with self.database.connect() as db:
            old=db.execute("SELECT * FROM video_history WHERE bvid='old'").fetchone()
            self.assertEqual((old['summary'],old['selection_reason'],old['coverage'],old['evidence_ids']),('','','[]','[]'))
            self.assertEqual((old['bvid'],old['status'],old['observations']),('old','sampled',4))
            self.assertIsNone(db.execute("SELECT 1 FROM video_selection_hints WHERE bvid='old'").fetchone())
            for bvid in ['edge','current','bookmark']:
                self.assertEqual(db.execute('SELECT summary FROM video_history WHERE bvid=?',(bvid,)).fetchone()[0],'summary')
            comments={r['bvid']:r for r in db.execute('SELECT * FROM video_comment_attempts')}
            self.assertEqual(comments['sent']['text'],'')
            self.assertEqual(comments['sent']['state'],'sent')
            for state in ['unknown','dispatching']:self.assertEqual(comments[state]['text'],'comment')
            self.assertEqual(len(comments),3) # account/BV duplicate check still matches forever.
        waiting=prune_activity(self.database,self.now+86399,scope='watcher')
        self.assertEqual(waiting['state'],'not_due')
        self.assertEqual(waiting['next_at'],self.now+86400)
        self.assertEqual(prune_activity(self.database,self.now+86400,scope='watcher')['state'],'pruned')

    def test_observation_denominator_audit_and_cost_ledger_survive(self):
        ledger=BudgetLedger(self.database)
        ledger.reserve('unresolved','.02',self.now)
        with self.database.connect(write=True) as db:
            db.executemany('INSERT INTO watcher_events VALUES (?,?,?)',[(0,'observed','{}'),(self.cutoff,'observed','{}'),(0,'model_role','billing-audit')])
        prune_activity(self.database,self.now,scope='watcher')
        with self.database.connect() as db:
            samples=db.execute("SELECT count(*) FROM watcher_events WHERE kind='observed'").fetchone()[0]+retained_observation_count(db)
            self.assertEqual(samples,2)
            self.assertEqual(db.execute("SELECT details FROM watcher_events WHERE kind='model_role'").fetchone()[0],'billing-audit')
            self.assertEqual(db.execute('SELECT state,reserved FROM budget').fetchone()[:],('reserved',20000))
        prune_activity(self.database,self.now+86400,scope='watcher')
        with self.database.connect() as db:self.assertEqual(retained_observation_count(db),2)

    def test_compacted_comment_still_rejects_duplicate_dispatch(self):
        from bili_companion.policy.video_comments import VideoCommentCandidate
        from types import SimpleNamespace
        with self.database.connect(write=True) as db:
            db.execute("INSERT INTO video_comment_attempts VALUES ('account','BVold','session','old comment',0,'old-day','sent','up')")
        prune_activity(self.database,self.now,scope='watcher')
        evidence=dict(session_id='fresh',attribution='observation',confidence=1,modality='audio',observed_at=self.now,text='钢琴演奏')
        executor=VideoCommentExecutor(self.database,SimpleNamespace(get=lambda identity,scope:evidence),enabled=True)
        candidate=VideoCommentCandidate('BVold','这一段钢琴演奏很清晰',('e',),self.now+10,1,'fresh')
        current=dict(state='WATCHING_VIDEO',live_session=None,watch_epoch=1,video=dict(bvid='BVold',session_id='fresh',observations=2,interest='high',uploader_id='up',comment_chunks=2,comment_coverage=[[0,60]]))
        def sender(*args,**kwargs):self.fail('duplicate must never send')
        self.assertEqual(executor.dispatch(candidate,lambda:current,sender,account_id='account',clock=lambda:self.now),'duplicate')

    def test_unresolved_selection_audit_is_preserved(self):
        ledger=BudgetLedger(self.database)
        ledger.reserve('pending','.02',self.now)
        with self.database.connect(write=True) as db:
            db.execute('CREATE TABLE video_selection_audit (request_id TEXT PRIMARY KEY,at REAL,bvid TEXT,reason TEXT,qq_context_json TEXT)')
            db.executemany('INSERT INTO video_selection_audit VALUES (?,0,?,?,?)',[('pending','BVpending','reason','[]'),('settled','BVold','reason','[]')])
        prune_activity(self.database,self.now,scope='watcher')
        with self.database.connect() as db:self.assertEqual([r[0] for r in db.execute('SELECT request_id FROM video_selection_audit')],['pending'])

    def test_unknown_schema_fails_closed_and_rolls_back(self):
        self.seed_video('old',0)
        with self.database.connect(write=True) as db:db.execute('ALTER TABLE watcher_events ADD COLUMN unexpected TEXT')
        self.assertEqual(prune_activity(self.database,self.now,scope='watcher')['state'],'failed')
        with self.database.connect() as db:self.assertEqual(db.execute('SELECT summary FROM video_history').fetchone()[0],'summary')

    def test_bridge_prunes_only_read_activity_not_memory(self):
        with self.database.connect(write=True) as db:
            db.execute('CREATE TABLE qq_memory_reads (id INTEGER PRIMARY KEY,read_at REAL,keyword TEXT,status TEXT,records_json TEXT)')
            db.executemany('INSERT INTO qq_memory_reads VALUES (?,?,?,?,?)',[(1,0,'key','first_import','[]'),(2,self.cutoff,'key','imported','[]')])
            db.execute('CREATE TABLE notes (id TEXT,text TEXT)')
            db.execute("INSERT INTO notes VALUES ('important','keep impression')")
        prune_activity(self.database,self.now,scope='bridge')
        with self.database.connect() as db:
            self.assertEqual(db.execute('SELECT id FROM qq_memory_reads').fetchall()[0][0],2)
            self.assertEqual(db.execute('SELECT text FROM notes').fetchone()[0],'keep impression')

if __name__=='__main__':unittest.main()
