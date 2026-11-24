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

if __name__=='__main__':unittest.main()
