from datetime import date
from pathlib import Path
import json,tempfile,unittest
from unittest.mock import patch
from hakimi.tools import genshin_version as version

def candidate(number,name='测试版本'):
    return {'version':number,'name':name,'source':'https://ys.mihoyo.com/main/m/news/detail/test','evidence':'隔离验收中的官方公告占位。','released_on':'2026-10-28'}

class VersionTests(unittest.TestCase):
    def test_monotonic_numeric_versions_and_shared_runtime_path(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);runtime=base/'official_test_runtime'
            self.assertEqual(version.current(runtime)['version'],'7.1')
            self.assertEqual(version.update(base,candidate('7.9'),today=date(2026,10,28))['status'],'updated')
            version.update(base,candidate('7.10'),today=date(2026,10,28))
            before=version.path(base).read_bytes()
            for number in ('7.1','7.9','7.10','6.8'):
                self.assertEqual(version.update(runtime,candidate(number,'旧标题'),today=date(2026,10,28))['status'],'kept')
            self.assertEqual(version.path(base).read_bytes(),before)
            self.assertEqual(version.current(runtime)['version'],'7.10')

    def test_future_release_keeps_live_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            item=candidate('7.2');item['released_on']='2026-10-29'
            with self.assertRaisesRegex(ValueError,'release_not_live'):version.update(d,item,today=date(2026,10,28))
            self.assertEqual(version.current(d)['version'],'7.1')

    def test_live_prompt_and_dsh_share_version_and_keep_other_groups_unchanged(self):
        from tests.test_official_adapter import fixture_root
        from hakimi.prompt.prompt_context import build_context,lore_guidance
        from hakimi.models.fallback_supervisor import fallback_prompt
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);fixture_root(root);base=root/'official_test_runtime'
            payload={'session_key':'group:850245632','trigger_id':'version-check','recent_context_messages':[{'user_id':'22','message_id':'1','text':'原神剧情考据'}]}
            with patch('prompt_context.resolve_profile',return_value=({'name':'lore_club'},[])):
                context=build_context(payload,base)
                self.assertIn('7.1《往冥府的安魂歌》',lore_guidance(context))
                self.assertIn('7.1',fallback_prompt(payload,base))
                version.update(root,candidate('7.2','新版本'),today=date(2026,10,28))
                context=build_context(payload,base)
                self.assertIn('7.2《新版本》',lore_guidance(context))
                self.assertIn('新版本',fallback_prompt(payload,base))
            with patch('prompt_context.resolve_profile',return_value=({'name':'study_calm'},[])):
                self.assertNotIn('genshin_current_version',build_context(payload,base))

if __name__=='__main__':unittest.main()
