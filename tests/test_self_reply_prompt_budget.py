import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hakimi.models.fallback_prompt import (_HistoryPrompt, append_fallback_context,
                                          fallback_prompt, MAX_PROMPT_BYTES, MAX_PROMPT_CHARS)


class SelfReplyPromptBudgetTests(unittest.TestCase):
    def test_byte_limit_drops_history_preserves_actual_tool_and_repair_suffix(self):
        original = 'INPUT_AND_REFERENCE:' + '入'*13000
        prompt = _HistoryPrompt(original+'OPTIONAL_OWN:'+'己'*2500, lambda: original)
        tool = '\nACTUAL_TOOL_RESULT:'+'工'*700
        with_tool = append_fallback_context(prompt,tool)
        self.assertIn('OPTIONAL_OWN',with_tool)
        repair = '\nEXACT_PROTOCOL_REPAIR:'+'修'*700
        final = append_fallback_context(with_tool,repair)
        self.assertEqual(final,original+tool+repair)
        self.assertLessEqual(len(final.encode('utf-8')),MAX_PROMPT_BYTES)

    def test_character_limit_is_independent_of_byte_limit(self):
        original='x'*23000
        prompt=_HistoryPrompt(original+'OPTIONAL_OWN:'+'y'*700,lambda:original)
        suffix='TOOL:'+('z'*500)
        final=append_fallback_context(prompt,suffix)
        self.assertEqual(final,original+suffix)
        self.assertLessEqual(len(final),MAX_PROMPT_CHARS)

    def test_original_overlong_tool_history_keeps_existing_rejection_behavior(self):
        original='BASE'
        prompt=_HistoryPrompt(original+'OPTIONAL',lambda:original)
        suffix='T'*MAX_PROMPT_CHARS
        self.assertEqual(append_fallback_context(prompt,suffix),original+suffix)
        self.assertGreater(len(original+suffix),MAX_PROMPT_CHARS)

    def test_lazy_snapshot_serialization_never_reloads_context_and_keeps_reference(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            base=Path(directory);(base/'state').mkdir()
            receipts=[dict(session_key='group:123',at=9900+i,message_id='self'+str(i),
                           text='OPTIONAL_CONFIRMED_'+str(i)+'己'*1500) for i in range(5)]
            (base/'state/sent_messages.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in receipts),encoding='utf-8')
            payload={'session_key':'group:123','recent_context_messages':[
                dict(session_key='group:123',message_id='q',user_id='7',received_at=10000,
                     reply_to='self0',text='ORIGINAL_INPUT:'+'入'*10000)]}
            from hakimi.models import fallback_prompt as module
            with patch.object(module,'build_context',wraps=module.build_context) as build:
                prompt=fallback_prompt(payload,base)
                self.assertEqual(build.call_count,1)
            self.assertIn('ORIGINAL_INPUT:',prompt)
            self.assertIn('reply_relationships',prompt)
            self.assertIn('self0',prompt)
            self.assertNotIn('OPTIONAL_CONFIRMED_',prompt)
            self.assertLessEqual(len(prompt.encode('utf-8')),MAX_PROMPT_BYTES)
            self.assertLessEqual(len(prompt),MAX_PROMPT_CHARS)


if __name__=='__main__':unittest.main()
