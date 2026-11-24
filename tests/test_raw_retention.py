"""Recent raw chat survives capacity pressure without changing retrieval limits."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from hakimi.memory.memory_store import MemoryStore


class RawRetentionTests(unittest.TestCase):
    def test_capacity_budget_preserves_all_active_chat_within_default_twenty_four_hours(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);now=200000
            (base/'config.json').write_text(json.dumps({'memory_policy':{
                'recent_original_days':7,'recent_original_limit':100,'per_session_minimum':0}}))
            index=MemoryStore(base)
            with index.db() as db:
                rows=[]
                for label,count,age in (('old',130,2*86400),('recent',150,1800)):
                    for i in range(count):
                        origin=label+str(i)
                        rows.append(('chat:group:1:'+origin,'chat','group:1','101','',
                                     'x',now-age-i,0,origin,''))
                db.executemany('INSERT INTO records(source_key,kind,session,user_id,key,text,at,expires,origin,source) VALUES (?,?,?,?,?,?,?,?,?,?)',rows)
            with patch.object(index,'backup_daily'):
                self.assertEqual(index.prune(now),30)
            with index.db() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM records WHERE origin LIKE 'recent%' AND text!=''").fetchone()[0],150)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM records WHERE origin LIKE 'old%' AND text!=''").fetchone()[0],100)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM records WHERE origin LIKE 'old%' AND text=''").fetchone()[0],30)


if __name__=='__main__':unittest.main()
