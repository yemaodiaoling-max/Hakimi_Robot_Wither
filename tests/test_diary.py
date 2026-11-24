import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from hakimi.features.diary_store import save,read_entry,memory,export_spec,submit_export,folder
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.bridge.delivery_guard import DeliveryGuard
from hakimi.models.fallback_runner import parse_response,FallbackError

class DiaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();(self.base/'state').mkdir()
        self.c=TurnCoordinator(self.base)

    def test_original_and_monthly_are_full_and_second_save_cannot_rewrite(self):
        text='SOURCE_ORIGINAL\n\n第二节，原文✨'
        save(self.base,'2026-10-26','标题',text,'夜色','gemini')
        save(self.base,'2026-10-26','不能覆盖','CHANGED','日出','dsh')
        self.assertEqual(read_entry(self.base,'2026-10-26')['text'],text)
        self.assertEqual((folder(self.base)/'2026-10-26/original.md').read_bytes(),text.encode())
        self.assertIn(text,(folder(self.base)/'monthly/2026-10.md').read_text(encoding='utf-8'))
        self.assertEqual(memory(self.base,'日记')[0]['day'],'2026-10-26')

    def test_dates_and_dsh_request_have_no_new_tool_authority(self):
        with self.assertRaises(ValueError):export_spec({'until':'../secret'})
        with self.assertRaises(ValueError):export_spec({'until':'2026-10-26','since':'2026-10-27'})
        self.assertEqual(parse_response('{"action":"reply","text":"","diary_export":{"until":"2026-10-26"}}')['diary_export']['until'],'2026-10-26')
        with self.assertRaises(FallbackError):parse_response('{"action":"reply","text":"","diary_export":{"file":"secret"}}')

    def test_pdf_exists_until_ack_and_is_deleted_after_delivery(self):
        save(self.base,'2026-10-26','标题','SOURCE_ORIGINAL','夜色','gemini')
        self.c.publish({'session_key':'group:123','trigger_id':'diary-test','recent_context_messages':[{'text':'发日记'}]})
        claim=self.c.claim_primary()
        guard=DeliveryGuard(self.base/'state/delivery_state.json',self.base/'state/outbox.jsonl')
        def compile_test(rows,outdir,title):
            path=Path(outdir)/'diary.pdf';path.write_bytes(b'%PDF-test');return path
        with patch('diary_latex.compile_pdf',side_effect=compile_test):
            result=submit_export(self.c,claim['token'],'gemini',{'until':'2026-10-26'},self.base)
        self.assertTrue(result['accepted'])
        files=list((self.base/'state/outgoing_files/group_123').glob('*.pdf'));self.assertEqual(len(files),1)
        self.assertIsNotNone(guard.next_message());guard.begin_attempt();guard.finish_attempt('offline')
        self.assertTrue(files[0].exists())
        guard.begin_attempt();guard.finish_attempt('success')
        self.assertFalse(files[0].exists())
        receipt=json.loads((self.base/'state/diary_receipts'/f"{result['reply_id']}.json").read_text())
        self.assertEqual(receipt['status'],'delivered')

if __name__=='__main__':unittest.main()
