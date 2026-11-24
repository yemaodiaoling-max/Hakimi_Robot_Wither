import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from hakimi.tools import qq_video


class TimelineCacheTests(unittest.TestCase):
    def test_old_prefix_cache_and_incomplete_attempt_are_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            (base/'state').mkdir()
            companion=base/'watcher.json'
            companion.write_text('{}',encoding='utf-8')
            (base/'video_tools_config.json').write_text(json.dumps({'companion_config':str(companion)}),encoding='utf-8')
            note=qq_video.register(base,'group:1','11',[dict(content_type='video/mp4',url='https://video.qq.com/a')])
            payload=dict(session_key='group:1',reply_target_message_id='11',
                         recent_context_messages=[dict(message_id='11',user_id='3',text=note)])
            database=sqlite3.connect(base/'state/qq_video.sqlite3')
            try:
                database.execute('UPDATE attachments SET report=?',(json.dumps({
                    'revision':'timeline-v1','report':{'ok':True,'observed_seconds':30}}),))
                database.commit()
            finally:
                database.close()
            partial={'ok':True,'unfinished_samples':2}
            sampled={'ok':True,'complete':False,'ending_seen':True,'unfinished_samples':0}
            with patch.object(qq_video.subprocess,'run',side_effect=[Mock(stdout=json.dumps(r)) for r in (partial,sampled)]) as worker:
                for expected in (partial,sampled,sampled):
                    self.assertEqual(qq_video.watch(base,payload,'11-0'),expected)
                self.assertEqual(worker.call_count,2)
