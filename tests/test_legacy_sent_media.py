import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from hakimi.transport.legacy_sent_media import legacy_media_segments


IMAGE = '[CQ:image,file=file:///bot/stickers/collected/example.jpg,sub_type=1]'
FILE = '[CQ:file,name=diary_export_00000000000000000000000000000000.pdf,stamp=1,mode=auto]'


class LegacySentMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base/'state'
        self.state.mkdir()
        self.batch = dict(session_key='group:42', trigger_id='turn', provider='dsh', created_at=1,
                          segments=['hello', IMAGE])
        self.acks = [dict(session_key='group:42',trigger_id='turn',provider='dsh',message_id=str(i),at=i+2,text=t)
                     for i,t in enumerate(('hello',''),start=1)]

    def recover(self):
        (self.state/'outbox.jsonl').write_text(json.dumps(self.batch)+'\n',encoding='utf-8')
        return legacy_media_segments(self.acks,self.base)

    def test_complete_receipts_in_any_input_order(self):
        self.acks.reverse()
        self.assertEqual(self.recover(),{('group:42','2'):IMAGE})

    def test_receipt_count_text_timestamp_and_provider_must_match(self):
        originals=self.acks
        for invalid in ([originals[1]], [dict(originals[0],text='changed'),originals[1]],
                        [originals[0],dict(originals[1],at=0)],
                        [dict(originals[0],provider='gemini'),originals[1]],
                        [originals[0],dict(originals[1],session_key='group:43')],
                        [originals[0],dict(originals[1],message_id='1')]):
            with self.subTest(invalid=invalid):
                self.acks=invalid
                self.assertEqual(self.recover(),{})

    def test_blank_or_substitutable_batches_are_not_evidence(self):
        self.acks[0]['text']=''
        for first in (FILE,IMAGE,''):
            with self.subTest(first=first):
                self.batch['segments'][0]=first
                self.assertEqual(self.recover(),{})

    def test_duplicate_turn_in_outbox_cannot_be_used(self):
        (self.state/'outbox.jsonl').write_text((json.dumps(self.batch)+'\n')*2)
        self.assertEqual(legacy_media_segments(self.acks,self.base),{})

    def test_exact_actual_segment_beats_substitutable_planned_file(self):
        self.batch['segments'][-1]=FILE
        with sqlite3.connect(self.state/'memory.sqlite3') as db:
            db.execute('create table reply_review_windows(session text,updated_at real,replies text)')
            db.execute('insert into reply_review_windows values(?,?,?)',('group:42',10,json.dumps([
                dict(speaker='SELF',message_id='2',text=IMAGE)])))
        db.close()
        self.assertEqual(self.recover(),{('group:42','2'):IMAGE})

    def test_incomplete_old_text_excerpt_does_not_anchor_image(self):
        self.batch['segments'][0]='z'*1501;self.acks[0]['text']='z'*1500
        self.assertEqual(self.recover(),{})
