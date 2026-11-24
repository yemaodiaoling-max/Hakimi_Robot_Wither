import json,sqlite3,tempfile,unittest
from pathlib import Path
from hakimi.console.message_log import recent_messages

class Sessions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.state=self.base/'state';self.state.mkdir()
        self.db=sqlite3.connect(self.state/'memory.sqlite3');self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY,kind TEXT,session TEXT,user_id TEXT,text TEXT,at REAL,origin TEXT,nickname TEXT,state TEXT)')
        self.db.executemany('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)',[
            (1,'chat','group:200','22','旧群原文',100,'1','原昵称','active'),
            (2,'lore','group:200','22','不能冒充聊天的记忆',999,'note','原昵称','active'),
            (3,'chat','group:200','22','已删除',999,'3','原昵称','deleted'),
            (4,'chat','private:22','22','私聊原文',110,'4','群友214','active')])
        self.db.commit()
        rows=[dict(session_key='group:100',message_id=str(1000+i),received_at=1000+i,text='活跃群',user_id='33') for i in range(400)]
        (self.state/'chat_history.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    def test_overview_keeps_quiet_group(self):
        received=[r for r in recent_messages(self.base) if r['kind']=='received']
        self.assertTrue(any(r['text']=='旧群原文' for r in received))
        self.assertFalse(any(r['text'] in ('已删除','不能冒充聊天的记忆') for r in received))
    def test_session_reads_archived_originals_only(self):
        rows=recent_messages(self.base,'group:200')
        self.assertTrue(rows);self.assertTrue(all(r['session']=='group:200' for r in rows))
        self.assertEqual(rows[0]['text'],'旧群原文');self.assertEqual(rows[0]['actor'],'原昵称')
    def test_live_copy_wins_without_duplicate(self):
        with (self.state/'chat_history.jsonl').open('a') as f:
            f.write(json.dumps(dict(session_key='group:200',message_id='1',received_at=100,text='实时原文',nickname='新昵称'))+'\n')
        rows=recent_messages(self.base,'group:200')
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['text'],'实时原文')
    def test_parameter_validation(self):
        for value in ('200',"group:1' OR 1=1",'group:0','../state'):
            with self.assertRaises(ValueError):recent_messages(self.base,value)
    def test_private_session_is_explicit(self):
        rows=recent_messages(self.base,'private:22')
        self.assertEqual([r['text'] for r in rows],['私聊原文'])
    def test_per_session_300_bound(self):
        self.db.executemany('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)',[
            (100+i,'chat','group:200','22','历史'+str(i),200+i,str(100+i),'群友214','active') for i in range(400)])
        self.db.commit();rows=recent_messages(self.base,'group:200')
        self.assertEqual(len(rows),300);self.assertEqual(rows[0]['text'],'历史399')

if __name__=='__main__':unittest.main()
