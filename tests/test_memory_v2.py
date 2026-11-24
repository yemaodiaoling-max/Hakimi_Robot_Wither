import contextlib,json,sqlite3,sys,tempfile,time,unittest
from pathlib import Path
here=Path(__file__).resolve().parent
if here.name=='tests':
    sys.path.insert(0,str(here.parent))
    sys.path.insert(0,str(here.parent/'official_test_runtime'))
else:sys.path.insert(0,str(here/'src'))
from hakimi.memory.memory_store import MemoryStore
from hakimi.memory.memory_lifecycle import explicit_person,sourced_reply,source_url
from hakimi.memory.smart_memory import save_memory,valid_memory
from hakimi.models.scoped_session import ScopedSession

class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.s=MemoryStore(self.base);self.now=time.time()
    def tearDown(self):self.tmp.cleanup()
    def chat(self,text,uid='101',session='group:1',mid='a',at=None):
        r=dict(text=text,user_id=uid,session_key=session,message_id=mid,received_at=at or self.now)
        self.s.observe(r);return r
    def note(self,text,key='课题',session='group:1',at=None,**extra):
        self.s.note(dict(kind='person',person_id='101',session_key=session,note=text,key=key,created_at=at or self.now,**extra))
    def search(self,q='你记得我吗',session='group:1',uid='101',**kw):
        return self.s.retrieve(session,[],dict(text=q,user_id=uid),now=self.now+10,**kw)
    def test_direct_statement_survives_raw_pruning_with_evidence(self):
        self.chat('我正在学声学',at=self.now-9*86400)
        self.s.prune(self.now)
        hits=self.search('我学什么');self.assertTrue(any('声学' in x for x in hits))
        with self.s.db() as d:
            r=d.execute("SELECT * FROM records WHERE kind='person'").fetchone()
            self.assertEqual(r['verification'],'self_report');self.assertEqual(r['evidence'],'我正在学声学')
            self.assertEqual(d.execute("SELECT text FROM records WHERE kind='chat'").fetchone()[0],'')
    def test_banter_forward_sensitive_not_promoted(self):
        for i,text in enumerate(['我喜欢猫','“叫我大王”','假如我正在学声学','[转发]我正在学声学','我正在学声学哈哈','记住，我喜欢萝莉']):
            self.chat(text,mid=str(i))
        with self.s.db() as d:self.assertEqual(d.execute("SELECT count(*) FROM records WHERE kind='person'").fetchone()[0],0)
    def test_correction_supersedes_and_read_labels_old(self):
        self.note('旧结论',at=self.now-100);self.note('新结论')
        self.assertFalse(any('旧结论' in x for x in self.search('旧结论')))
        with self.s.db() as d:old=d.execute("SELECT id FROM records WHERE text='旧结论'").fetchone()[0]
        r=self.s.read('M'+str(old),'group:1',['101']);self.assertEqual(r['status'],'superseded');self.assertIsNotNone(r['superseded_by'])
    def test_automatic_local_but_tools_cross_group(self):
        self.chat('这里讨论颅骨声学和超声穿透',uid='999',session='group:2')
        self.assertFalse(any('颅骨' in x for x in self.search('颅骨')))
        hits=self.search('头骨声音传播',cross_groups=True)
        self.assertTrue(any('颅骨' in x and 'group:2' in x for x in hits))
        with self.s.db() as d:rid=d.execute("SELECT id FROM records WHERE kind='chat'").fetchone()[0]
        self.assertIsNone(self.s.read('M'+str(rid),'group:1',[]))
        self.assertIsNotNone(self.s.read('M'+str(rid),'group:1',[],cross_groups=True))
    def test_cross_group_recalls_same_person_private(self):
        self.chat('我正在学声学',session='private:101')
        self.assertTrue(self.search('声学',cross_groups=True))
    def test_public_self_card_shared_for_same_account(self):
        self.chat('叫我阿蓝',session='group:2')
        self.assertTrue(any('阿蓝' in x for x in self.search()))
        self.assertFalse(any('阿蓝' in x for x in self.search(uid='102')))
    def test_sourced_delivered_answer_becomes_lore(self):
        self.s.dialogue_signature('group:1','t1','足迹预告是什么','《足迹》是原神提瓦特篇的主线章节预告，发布页面可以核对视频的名称与章节。 https://www.bilibili.com/video/BV1At4y1q7UQ/',self.now)
        with self.s.db() as d:
            r=d.execute("SELECT * FROM records WHERE kind='lore'").fetchone()
            self.assertEqual(r['verification'],'answer_unverified');self.assertIn('足迹',r['evidence'])
        self.assertTrue(any('足迹' in x and 'https://' in x for x in self.search('足迹')))
    def test_attachment_secret_url_never_knowledge(self):
        for url in ['https://127.0.0.1/x','https://localhost/x','https://example.com/x?token=secret','https://multimedia.nt.qq.com.cn/x?rkey=a']:
            self.assertEqual(source_url(url),'')
        self.assertFalse(sourced_reply('group:1','x','q','长一些的文字用于说明这个附件并不是知识资料的合法出处 https://qq.ugcimg.cn/test',self.now))
    def test_no_source_no_automatic_fact(self):
        self.s.dialogue_signature('group:1','a','题目','这是机器人自信但没有任何来源支持的回答。',self.now)
        with self.s.db() as d:self.assertEqual(d.execute("SELECT count(*) FROM records WHERE kind='lore'").fetchone()[0],0)
    def test_substantial_episode_retained_as_event_not_fact(self):
        self.s.dialogue_signature('group:1','t','我们一起讨论一下这个声学实验的研究方案','这是一段已经发出的实验讨论。'*12,self.now)
        self.s.prune(self.now+3600)
        with self.s.db() as d:
            row=d.execute("SELECT * FROM records WHERE kind='episode'").fetchone()
            self.assertEqual(row['verification'],'observed_dialogue')
            self.assertIn('不保证结论正确',row['text']);self.assertGreater(row['expires'],self.now+100*86400)
    def test_repeat_is_idempotent(self):
        row=self.chat('叫我阿蓝');self.s.observe(row)
        self.s.consolidate();self.s.consolidate()
        with self.s.db() as d:self.assertEqual(d.execute("SELECT count(*) FROM records WHERE kind='person'").fetchone()[0],1)
    def test_fair_retention(self):
        (self.base/'config.json').write_text(json.dumps({'memory_policy':{'recent_original_limit':100,'per_session_minimum':20}}))
        for i in range(20):self.chat('安静群的原话',session='group:2',mid=str(i),at=self.now-1000+i)
        for i in range(130):self.chat('热闹群的原话',mid=str(i),at=self.now-500+i)
        self.s.prune(self.now)
        with self.s.db() as d:
            self.assertEqual(d.execute("SELECT count(*) FROM records WHERE kind='chat' AND text!=''").fetchone()[0],100)
            self.assertEqual(d.execute("SELECT count(*) FROM records WHERE session='group:2' AND text!=''").fetchone()[0],20)
    def test_budget_and_original_reader(self):
        for i in range(8):self.note('量子研究'+str(i)*100,key=str(i))
        hits=self.search('量子研究');self.assertLessEqual(len(hits),4);self.assertLessEqual(sum(map(len,hits)),600)
    def test_expired_hidden_and_read_explicit(self):
        self.note('应过期',at=self.now-400*86400)
        self.assertFalse(self.search('应过期'))
        with self.s.db() as d:rid=d.execute('SELECT id FROM records').fetchone()[0]
        self.assertEqual(self.s.read('M'+str(rid),'group:1',['101'])['status'],'expired')
    def test_validated_writer_retains_evidence(self):
        proposal={'kind':'lore','key':'足迹','note':'足迹是主线章节预告','source':'https://www.bilibili.com/video/BV1At4y1q7UQ/','evidence':'主线章节预告PV'}
        self.assertTrue(valid_memory(proposal));self.assertTrue(save_memory(self.base,'group:1',proposal,{},now=self.now))
        with self.s.db() as d:
            row=d.execute("SELECT * FROM records WHERE kind='lore'").fetchone();self.assertEqual(row['evidence'],proposal['evidence']);self.assertEqual(row['verification'],'source_excerpt')
    def test_daily_backup_is_consistent_and_bounded(self):
        self.chat('只保存在原始消息中的内容')
        for day in range(9):self.s.backup_daily(self.now+day*86400)
        files=list((self.base/'state/memory_backups').glob('*.sqlite3'));self.assertEqual(len(files),1)
        with contextlib.closing(sqlite3.connect(files[-1])) as d:
            self.assertEqual(d.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(d.execute('SELECT count(*) FROM records').fetchone()[0],1)

class Fake:
    made=[]
    def __init__(self,*args,state_path=None,resume=False,**kw):self.state_path=state_path;self.turns=0;self.resume=None;self.closed=[];Fake.made.append(self)
    def close(self,status='closed'):self.closed.append(status)
    def generate(self,*args):self.turns+=1;return {'status':'SUCCESS'}

class SessionTests(unittest.TestCase):
    def test_single_live_process_separate_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            Fake.made=[];pool=ScopedSession([],tmp,{},Path(tmp)/'sessions',factory=Fake)
            for key in ['group:1','group:1','group:2','group:1']:
                pool.generate('',None,{'payload':{'session_key':key}})
            self.assertEqual(len(Fake.made),3)
            self.assertEqual(Fake.made[0].state_path,Fake.made[2].state_path)
            self.assertNotEqual(Fake.made[0].state_path,Fake.made[1].state_path)
            self.assertEqual(Fake.made[0].closed,['ready']);self.assertEqual(Fake.made[1].closed,['ready'])
    def test_invalid_scope_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool=ScopedSession([],tmp,{},tmp,factory=Fake)
            with self.assertRaises(ValueError):pool.generate('',None,{'payload':{'session_key':'../../x'}})

if __name__=='__main__':unittest.main()
