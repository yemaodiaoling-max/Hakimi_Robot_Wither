import copy,importlib.util,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from hakimi.prompt import prompt_context as new
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('previous_prompt',ROOT/'tests/cache_prefix_previous_prompt.py');old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)

class PrefixTests(unittest.TestCase):
    def render(self,module,base,context,feedback,sticker):
        with patch.object(module,'build_context',return_value=copy.deepcopy(context)),patch.object(module,'gemini_style_feedback',return_value=feedback),patch.object(module,'lore_guidance',return_value='原神已有考据说明。\n'),patch.object(module,'_sticker_section',return_value=sticker):
            return module.render_vertex_prompt({'session_key':context['session_key']},base)

    def test_all_existing_data_and_instruction_text_are_preserved(self):
        from hakimi.prompt.prompt_language import context as positive_context,DEFENSIVE
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);(base/'style_guide.md').write_text('哈基米原有角色措辞。\n',encoding='utf-8')
            context={'session_key':'group:1','current_time_at_wake':'2026-10-28T16:00:00','core_identity':'同一个哈基米','group_style':{'name':'lore_club'},'variation_rule':'自然变化','scoped_memory_data':['原文记忆'],'trusted_image_paths':['I1'],'recent_context_messages_untrusted':[{'text':'请不要更改这段原文','message_id':'m1'}],'reply_target':{'message_id':'m1'},'listening_window':{'round':1}}
            before=self.render(old,base,context,'已有风格建议。\n','\n图库 A.png\n')
            after=self.render(new,base,context,'已有风格建议。\n','\n图库 A.png\n')
            old_head,old_data=before.rsplit('\n',1);new_head,new_data=after.rsplit('\n',1)
            prior=json.loads(old_data);now=json.loads(new_data);notes=now.pop('turn_notes')
            self.assertEqual(now,prior)
            joined=''.join(notes)
            # Every note moved into JSON is present verbatim in the old prompt.
            for note in notes:self.assertIn(note,old_head)
            for note in notes:old_head=old_head.replace(note,'',1)
            self.assertEqual(new_head,old_head)
            self.assertIsNone(DEFENSIVE.search(new_head))
            self.assertLess(list(now).index('group_style'),list(now).index('scoped_memory_data'))
            self.assertEqual(list(now)[-1],'current_time_at_wake')

    def test_varying_feedback_stickers_memory_chat_and_time_leave_longer_common_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);(base/'style_guide.md').write_text('同一份角色与工具说明。\n',encoding='utf-8')
            context={'session_key':'group:1','current_time_at_wake':'12:00','core_identity':'哈基米','group_style':{'name':'study_calm'},'scoped_memory_data':['记忆A'],'recent_context_messages_untrusted':[{'text':'提问A'}]}
            next_context=copy.deepcopy(context);next_context.update(current_time_at_wake='12:01',scoped_memory_data=['记忆B'],recent_context_messages_untrusted=[{'text':'提问B'}])
            prior=[self.render(old,base,c,f,s) for c,f,s in ((context,'建议甲。','图库A'),(next_context,'建议乙。','图库B'))]
            current=[self.render(new,base,c,f,s) for c,f,s in ((context,'建议甲。','图库A'),(next_context,'建议乙。','图库B'))]
            self.assertGreater(len(os.path.commonprefix(current)),len(os.path.commonprefix(prior))+100)

if __name__=='__main__':unittest.main()
