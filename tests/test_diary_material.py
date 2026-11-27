import json,sqlite3,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from hakimi.features import diary_daily as diary

class MaterialTests(unittest.TestCase):
 def test_missing_database_stays_missing(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(diary,'ROOT',Path(tmp)):
   self.assertEqual(diary.diary_material('2026-11-05'),[])
   self.assertEqual(list(Path(tmp).iterdir()),[])

 def test_only_recent_public_originals_read_only_and_bounded(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(diary,'ROOT',Path(tmp)),patch.object(diary.time,'time',return_value=datetime(2026,11,5,23,tzinfo=diary.ZONE).timestamp()):
   p=Path(tmp)/'official_test_runtime/state/memory.sqlite3';p.parent.mkdir(parents=True)
   db=sqlite3.connect(p)
   db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY,kind,state,session,user_id,text,at,origin,self_id,direct_targets,reply_to,expires)')
   now=datetime(2026,11,5,12,tzinfo=diary.ZONE).timestamp()
   def put(i,session='group:1',at=now,user='1',state='active',origin='yes',text='今天提问中有一个具体疑惑值得记下。'):
    db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(i,'chat',state,session,user,text,at,origin,'','', '',0))
   put(1);put(2,user='SELF',at=now+10,text='我换了一个具体例子解释，仍然保留一个问题。')
   put(3,session='private:1');put(4,at=now-8*86400);put(5,at=now+86400);put(6,state='retracted');put(7,origin='')
   db.commit();db.close();before=p.read_bytes()
   result=diary.diary_material('2026-11-05');messages=[m for r in result for m in r['messages']]
   self.assertEqual({m['source'] for m in messages},{'M1','M2'})
   self.assertIn('自己',[m['speaker'] for m in messages])
   self.assertTrue(all('2026-11-05' in m['received_at'] for m in messages))
   self.assertEqual(p.read_bytes(),before)
   self.assertLessEqual(sum(len(m['text']) for m in messages),3600)

class WeightedTests(unittest.TestCase):
 def test_seeded_weighted_selection_keeps_best_and_deduplicates_events(self):
  from hakimi.features.diary_material import choose,seed_for_day
  candidates=[{'row':{'id':i,'session':'group:'+str(i%3),'user_id':str(i),'at':1000*i},'score':float(i)} for i in range(1,15)]
  first=choose(candidates,'2026-11-05');second=choose(candidates,'2026-11-05')
  self.assertEqual([c['row']['id'] for c in first],[c['row']['id'] for c in second])
  self.assertEqual(first[0]['row']['id'],14)
  self.assertEqual(len({c['row']['id'] for c in first}),3)
  self.assertNotEqual(seed_for_day('2026-11-05'),seed_for_day('2026-11-06'))
  same=[{'row':{'id':i,'session':'group:1','user_id':str(i),'at':i},'score':1} for i in range(5)]
  self.assertEqual(len(choose(same,'2026-11-05')),1)

 def test_calendar_buckets_cover_dst_day_with_bounded_queries(self):
  from hakimi.features.diary_material import candidate_rows
  class Database:
   def __init__(self):self.calls=[]
   def execute(self,sql,params):self.calls.append((sql,params));return self
   def fetchall(self):return []
  db=Database();end=datetime(2026,11,2,tzinfo=diary.ZONE).timestamp()
  candidate_rows(db,'2026-11-01',end)
  self.assertEqual(sum(p[-1] for _,p in db.calls),224)
  self.assertEqual(sum(p[2]-p[1] for _,p in db.calls[:4]),25*3600)
  self.assertTrue(all('RANDOM' not in sql.upper() for sql,_ in db.calls))

 def test_scores_reward_evidence_without_emotion_or_unbounded_length(self):
  from hakimi.features.diary_material import score,involved
  base={'text':'我想知道这个问题如何解释，能否用一个具体例子？','user_id':'1','self_id':'12','direct_targets':'[112]','reply_to':'','origin':'a','at':100}
  self.assertFalse(involved(base))
  tagged=dict(base,direct_targets='[12]')
  self.assertGreater(score(tagged,[tagged],100,[]),score(base,[base],100,[]))
  old=dict(base,at=100-6*86400)
  self.assertGreater(score(base,[base],100,[]),score(old,[old],100,[]))
  long=dict(base,text='具体解释'*500)
  longer=dict(base,text='具体解释'*1000)
  self.assertEqual(score(long,[long],100,[]),score(longer,[longer],100,[]))

class DisplayNameTests(unittest.TestCase):
 def _setup_db(self, tmp):
  p=Path(tmp)/'official_test_runtime/state/memory.sqlite3'
  p.parent.mkdir(parents=True,exist_ok=True)
  db=sqlite3.connect(p)
  db.execute('CREATE TABLE records(id INTEGER PRIMARY KEY,kind,state,session,user_id,key,text,at,origin,self_id,direct_targets,reply_to,expires,nickname)')
  db.execute('CREATE TABLE graph_aliases(node_id,alias,session,source_record_id,first_seen,last_seen)')
  return p,db

 def test_nickname_and_same_group_backfill(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(diary,'ROOT',Path(tmp)),patch.object(diary.time,'time',return_value=datetime(2026,11,5,23,tzinfo=diary.ZONE).timestamp()):
   _,db=self._setup_db(tmp)
   now=datetime(2026,11,5,12,tzinfo=diary.ZONE).timestamp()
   db.execute("INSERT INTO records VALUES (1,'chat','active','group:1','10001','','今天提问中有一个具体疑惑值得记下。',?,'o1','99999','[]','',0,'阿橘')",(now,))
   db.execute("INSERT INTO records VALUES (2,'chat','active','group:1','10002','','之前我在这群发过一条旧消息。',?,'o0','99999','[]','',0,'小星')",(now-1000,))
   db.execute("INSERT INTO records VALUES (3,'chat','active','group:1','10002','','后来我这条消息没有附带群名片，但还是想追问一下。',?,'o2','99999','[]','o1',0,'')",(now+10,))
   db.commit();db.close()
   result=diary.diary_material('2026-11-05')
   messages=[m for r in result for m in r['messages']]
   speakers={m['source']:m['speaker'] for m in messages}
   self.assertEqual(speakers['M1'],'阿橘')
   self.assertEqual(speakers['M3'],'小星')
   self.assertIn('speaker为群昵称/称呼',result[0]['scope'])

 def test_numeric_nickname_rejected_and_falls_back_safely(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(diary,'ROOT',Path(tmp)),patch.object(diary.time,'time',return_value=datetime(2026,11,5,23,tzinfo=diary.ZONE).timestamp()):
   _,db=self._setup_db(tmp)
   now=datetime(2026,11,5,12,tzinfo=diary.ZONE).timestamp()
   db.execute("INSERT INTO records VALUES (1,'chat','active','group:1','20001','','纯数字昵称不能直接暴露，应该被过滤掉对吧？',?,'o1','99999','[]','',0,'20001')",(now,))
   db.execute("INSERT INTO records VALUES (2,'chat','active','group:1','20002','','占位昵称用户数字也不该直接当作正式称呼。',?,'o2','99999','[]','o1',0,'用户20002')",(now+10,))
   db.execute("INSERT INTO graph_aliases VALUES ('person:20001','团子','group:1',1,?,?)",(now,now))
   db.commit();db.close()
   result=diary.diary_material('2026-11-05')
   messages=[m for r in result for m in r['messages']]
   speakers={m['source']:m['speaker'] for m in messages}
   self.assertEqual(speakers['M1'],'团子')
   self.assertEqual(speakers['M2'],'群友1')
   dumped=json.dumps(result,ensure_ascii=False)
   self.assertNotIn('20001',dumped)
   self.assertNotIn('20002',dumped)

 def test_cq_at_mentions_cleaned_without_numeric_qq(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(diary,'ROOT',Path(tmp)),patch.object(diary.time,'time',return_value=datetime(2026,11,5,23,tzinfo=diary.ZONE).timestamp()):
   _,db=self._setup_db(tmp)
   now=datetime(2026,11,5,12,tzinfo=diary.ZONE).timestamp()
   db.execute("INSERT INTO records VALUES (1,'chat','active','group:1','30001','','刚才 [CQ:at,qq=99999] 和 [CQ:at,qq=30002] 还有 [CQ:at,qq=30003,name=小星] 以及 [CQ:at,qq=30004] 都在讨论这个问题。',?,'o1','99999','[]','',0,'阿橘')",(now,))
   db.execute("INSERT INTO records VALUES (2,'chat','active','group:1','30002','','我也觉得这个例子说得很清楚。',?,'o2','99999','[]','o1',0,'团子')",(now+10,))
   db.commit();db.close()
   result=diary.diary_material('2026-11-05')
   m1_text=[m['text'] for r in result for m in r['messages'] if m['source']=='M1'][0]
   self.assertIn('@自己',m1_text)
   self.assertIn('@团子',m1_text)
   self.assertIn('@小星',m1_text)
   self.assertIn('@群友',m1_text)
   for raw_id in ('99999','30002','30003','30004'):
    self.assertNotIn(raw_id,m1_text)

 def test_private_aliases_isolated_and_group_aliases_extracted(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(diary,'ROOT',Path(tmp)),patch.object(diary.time,'time',return_value=datetime(2026,11,5,23,tzinfo=diary.ZONE).timestamp()):
   _,db=self._setup_db(tmp)
   now=datetime(2026,11,5,12,tzinfo=diary.ZONE).timestamp()
   db.execute("INSERT INTO records VALUES (1,'chat','active','group:1','40001','','今天公开群里讨论的这个话题很有意思，大家怎么看？',?,'o1','99999','[]','',0,'阿橘')",(now,))
   db.execute("INSERT INTO graph_aliases VALUES ('person:40001','私聊秘密称呼','private:40001',10,?,?)",(now,now))
   db.execute("INSERT INTO graph_aliases VALUES ('person:40001','橘子','group:1',11,?,?)",(now,now))
   db.execute("INSERT INTO records VALUES (2,'person','active','group:1','40001','外号','大橘猫',?, 'p1','','[]','',0,'')",(now,))
   db.execute("INSERT INTO records VALUES (3,'person','active','private:40001','40001','称呼','私聊外号',?, 'p2','','[]','',0,'')",(now,))
   db.commit();db.close()
   result=diary.diary_material('2026-11-05')
   self.assertEqual(len(result),1)
   aliases=result[0].get('speaker_aliases',{}).get('阿橘',[])
   self.assertEqual(aliases,['橘子','大橘猫'])
   dumped=json.dumps(result,ensure_ascii=False)
   self.assertNotIn('私聊秘密称呼',dumped)
   self.assertNotIn('私聊外号',dumped)

 def test_owner_id_uses_owner_call_and_keeps_group_nickname_in_aliases(self):
  from hakimi.core import owner
  with tempfile.TemporaryDirectory() as tmp,patch.object(diary,'ROOT',Path(tmp)),patch.object(diary.time,'time',return_value=datetime(2026,11,5,23,tzinfo=diary.ZONE).timestamp()),patch.object(owner,'owner_ids',return_value=frozenset({'50001'})),patch.object(owner,'short_name',return_value='主人'):
   _,db=self._setup_db(tmp)
   now=datetime(2026,11,5,12,tzinfo=diary.ZONE).timestamp()
   db.execute("INSERT INTO records VALUES (1,'chat','active','group:1','50001','','今天在群里测试一下这个有意思的问题。',?,'o1','99999','[]','',0,'星河')",(now,))
   db.commit();db.close()
   result=diary.diary_material('2026-11-05')
   messages=[m for r in result for m in r['messages']]
   self.assertEqual(messages[0]['speaker'],'主人')
   self.assertEqual(result[0].get('speaker_aliases',{}).get('主人'),['星河'])

 def test_prompts_prefer_nicknames_and_forbid_identity_privacy(self):
  from hakimi import persona
  from hakimi.models import antigravity_tasks,provider_health
  writer_prompt=persona.text('diary_writer.txt')
  self.assertIn('昵称',writer_prompt)
  self.assertIn('外号',writer_prompt)
  self.assertNotIn('匿名，不写身份隐私',writer_prompt)
  captured={}
  def fake_diary_text(model,prompt,timeout):
   captured['prompt']=prompt
   return json.dumps({'title':'测试','text':'正文','image_prompt':'配图描述'},ensure_ascii=False)
  with tempfile.TemporaryDirectory() as tmp,patch.object(diary,'ROOT',Path(tmp)),patch.object(antigravity_tasks,'settings',return_value={'diary':'test-model'}),patch.object(provider_health,'availability',return_value={'mode':'gemini'}),patch.object(antigravity_tasks,'diary_text',side_effect=fake_diary_text),patch.object(diary,'save',return_value={'status':'ok'}):
   diary.generate('2026-11-05')
  self.assertIn('优先用材料里的昵称、外号或熟悉称呼',captured['prompt'])
  self.assertNotIn('匿名使用',captured['prompt'])

if __name__=='__main__':unittest.main()
