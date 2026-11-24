import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from bili_companion.domain import Evidence, Scope
from bili_companion.integrations.qq import QQSource
from bili_companion.integrations.qq_context import context_for
from bili_companion.integrations.service import tick, write_snapshot, LocalTransport
from bili_companion.memory import MemoryStore
from bili_companion.runtime.storage import Database
from bili_companion.sync import SourceItem, SourceBatch, SyncStore, SyncWorker


class QQIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp_root=Path(__file__).resolve().parents[1]/'state'/'test_tmp'
        temp_root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=temp_root)
        self.root=Path(self.temp.name)
        self.now=1_800_000_000
        self.native=self.root/'qq.sqlite3'
        with closing(sqlite3.connect(self.native)) as db, db:
            db.execute('''CREATE TABLE records(id INTEGER PRIMARY KEY,kind TEXT,session TEXT,
                user_id TEXT,text TEXT,at REAL,source TEXT,shared INTEGER,state TEXT,
                channel TEXT,model TEXT,expires REAL)''')
        self.qq=SyncStore(Database(self.root/'qq_side.sqlite3'),endpoint='qq',local_id='q',peer_id='b',clock=lambda:self.now)
        self.bili=SyncStore(Database(self.root/'bili_side.sqlite3'),endpoint='bilibili',local_id='b',peer_id='q',clock=lambda:self.now)

    def tearDown(self):
        self.temp.cleanup()

    def row(self, identity, **kwargs):
        row=dict(id=identity,kind='lore',session='group:1',user_id='',text='公开规则说明',
            at=self.now-5,source='https://example.org/public',shared=1,state='active',
            channel='gemini',model='gemini',expires=0)
        row.update(kwargs)
        with closing(sqlite3.connect(self.native)) as db, db:
            db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',tuple(row.values()))

    def test_allowlist_never_exports_private_shared_person_or_unsigned_self(self):
        self.row(1)
        self.row(2,kind='person',text='SHARED_PERSON_SECRET')
        self.row(3,session='private:2',text='PRIVATE_SECRET')
        self.row(4,kind='chat',text='CHAT_SECRET')
        self.row(5,kind='reflection',text='AMBIGUOUS_SELF_SECRET')
        self.row(6,kind='reflection',user_id='SELF',text='自己的阅读偏好')
        self.row(7,source='https://example.org/public?token=credential')
        self.row(8,expires=self.now-1)
        self.row(9,channel='memory_bridge')
        source=QQSource(self.native,self.qq)
        items=source('').items
        self.assertEqual({i.source_id for i in items},{'qq:M1','qq:M6'})
        self.assertNotIn('SECRET',repr(items))
        with closing(sqlite3.connect(self.native)) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM records').fetchone()[0],9)

    def test_expiry_retract_and_restore_higher_revision(self):
        self.row(1,expires=self.now+10)
        source=QQSource(self.native,self.qq)
        worker=SyncWorker(self.qq,source,LocalTransport(self.bili))
        self.assertEqual(worker.run_once(force=True)['state'],'synced')
        self.assertEqual(len(self.bili.recall()),1)
        self.now+=11
        self.assertEqual(worker.run_once(force=True)['state'],'synced')
        self.assertEqual(self.bili.recall(),[])
        with closing(sqlite3.connect(self.native)) as db, db:
            db.execute('UPDATE records SET expires=0 WHERE id=1')
        worker.run_once(force=True)
        self.assertEqual(self.bili.recall()[0]['revision'],2)

    def test_bidirectional_tick_dedicated_real_native_and_no_chat_mutation(self):
        self.row(1)
        native=self.root/'native_watch.sqlite3'
        memory=MemoryStore(Database(native))
        memory.append(Evidence('e1',Scope('b','live_room','8729095131'),'session',
            '听到钢琴片段，画面未知',self.now-2,0,4,'audio','observation'))
        memory.append(Evidence('title',Scope('b','live_room','8729095131'),'session',
            '标题不等于观看',self.now-2,0,4,'metadata','observation'))
        memory.append(Evidence('other',Scope('OTHER','live_room','8729095131'),'session',
            '其他账号隔离',self.now-2,0,4,'audio','observation'))
        cfg=dict(state_dir=str(self.root/'state'),qq_identity='q',bilibili_identity='b',
            qq_database=str(self.native),bilibili_database=str(native),bilibili_account_id='b',
            qq_snapshot=str(self.root/'state/bili_bridge_context.json'))
        result=tick(cfg,clock=lambda:self.now)
        self.assertEqual(result['model_calls'],0)
        self.assertEqual(result['qq_context_records'],1)
        context=context_for(self.root,'钢琴',self.now)
        self.assertEqual(context['bridge_memory_data'][0]['coverage'][0]['modality'],'audio')
        self.assertNotIn('标题',json.dumps(context,ensure_ascii=False))
        for query in ('刚才在B站看了什么', '直播'):
            recalled = context_for(self.root, query, self.now)['bridge_memory_data']
            self.assertTrue(recalled)
            self.assertTrue(recalled[0]['summary'].startswith('[B站·直播] '))
            self.assertNotIn('·视频', recalled[0]['summary'])
        tick(cfg, force=True, clock=lambda:self.now)
        repeated = context_for(self.root, '直播', self.now)['bridge_memory_data']
        self.assertEqual(repeated[0]['summary'].count('[B站·直播]'), 1)
        self.assertEqual(tick(cfg,clock=lambda:self.now)['qq']['state'],'not_due')
        with closing(sqlite3.connect(self.native)) as db, db:
            self.assertEqual(db.execute('SELECT count(*) FROM records').fetchone()[0],1)

    def test_snapshot_watermark_does_not_restore_expired_old_revision(self):
        from bili_companion.sync import seal_packet
        def packet(rev, expires):
            identity=dict(source_id='watched',revision=rev,scope='self')
            fact=dict(**identity,text='钢琴',source_refs=['bilibili:live:8729095131'],
                coverage=[dict(start=0,end=1,modality='audio')],attribution='observation',
                created_at=self.now-1,importance=0,truncated=False)
            return seal_packet(dict(version=1,packet_id='p'+str(rev),origin='bilibili',recipient='qq',
                origin_id='b',recipient_id='q',kind='watch_digest',created_at=self.now,
                expires_at=expires,source_ids=[identity],facts=[fact]))
        self.qq.receive(packet(1,self.now+100))
        self.qq.receive(packet(2,self.now+1))
        self.now+=2
        self.qq.cleanup()
        self.assertEqual(write_snapshot(self.qq,self.root/'state/bili_bridge_context.json'),0)
        self.assertEqual(context_for(self.root,'',self.now),{})

    def test_hook_checks_all_files_before_any_write_and_rolls_back(self):
        path=Path(__file__).resolve().parents[1]/'scripts/qq_hook.py'
        spec=importlib.util.spec_from_file_location('qq_hook_test',path)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        target=self.root/'prompt.py'; helper=self.root/'helper.py'; stage=self.root/'stage'
        stage.mkdir(); target.write_text('old prompt')
        (stage/'prompt_context.py.before').write_text('old prompt')
        (stage/'prompt_context.py.new').write_text('new prompt')
        (stage/'bridge_context.py.new').write_text('new helper')
        manifest=dict(target_before=module.sha(target),helper_before=None,
            target_after=module.sha(stage/'prompt_context.py.new'),helper_after=module.sha(stage/'bridge_context.py.new'))
        (stage/'manifest.json').write_text(json.dumps(manifest))
        with patch.object(module,'TARGET',target),patch.object(module,'HELPER',helper):
            (stage/'bridge_context.py.new').write_text('damaged helper')
            with self.assertRaises(ValueError):module.activate(stage)
            self.assertEqual(target.read_text(),'old prompt'); self.assertFalse(helper.exists())
            (stage/'bridge_context.py.new').write_text('new helper')
            module.activate(stage)
            self.assertEqual(target.read_text(),'new prompt'); self.assertEqual(helper.read_text(),'new helper')
            module.activate(stage,rollback=True)
            self.assertEqual(target.read_text(),'old prompt'); self.assertFalse(helper.exists())


if __name__=='__main__':
    unittest.main()
