import html,json,re,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from hakimi.chat.reply_format import limit_group_reply,text_bubbles
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.models.provider_health import ProviderHealth
from hakimi.prompt.prompt_context import resolve_profile
from hakimi.bridge import bridge_daemon as bridge
from tests.test_conversation_scheduler import config
from tests.test_delivery_guard import OfflineEventLoop

G='1142791485379513241'
RULE={'proactive_probability':.4,'max_chars':40,'max_messages':2}
CFG={'group_reply_overrides':{G:RULE}}
PROACTIVE_CFG={'group_reply_overrides':{G:{'proactive_probability':.4}}}

class LimitsTests(unittest.TestCase):
    def test_probability_only_preserves_complete_text_and_media(self):
        segments=['[CQ:reply,id=1]'+text_bubbles('好'*60)[0],text_bubbles('&#[]'*20)[0]]
        segments += ['[CQ:image,file='+str(i)+']' for i in range(6)]
        self.assertIs(limit_group_reply(segments,'group:'+G,PROACTIVE_CFG),segments)

    def test_probability_only_keeps_profile_and_both_provider_replies_complete(self):
        for provider in ('gemini','dsh'):
            with self.subTest(provider=provider),tempfile.TemporaryDirectory() as folder:
                base=Path(folder);(base/'config.json').write_text(json.dumps(PROACTIVE_CFG))
                (base/'group_profiles.json').write_text(json.dumps({'default_profile':'cat','profiles':{'cat':{'tone':'保持猫娘口吻','length':'按内容自然展开'}}}))
                profile,_=resolve_profile('group:'+G,base)
                self.assertEqual(profile['tone'],'保持猫娘口吻')
                self.assertEqual(profile['length'],'按内容自然展开')
                c=TurnCoordinator(base)
                if provider=='dsh':ProviderHealth(base).set_mode('dsh')
                c.publish({'session_key':'group:'+G,'trigger_id':'unlimited-'+provider})
                claim=c.claim_fallback() if provider=='dsh' else c.claim_primary()
                segments=['字'*35,'啊'*30,'第三条']
                self.assertTrue(c.submit_reply(claim['token'],segments,provider)['accepted'])
                record=json.loads((base/'state/outbox.jsonl').read_text())
                self.assertEqual(record['segments'],segments)

    def test_budget_is_total_preserves_media_and_cq_and_other_sessions(self):
        segments=['[CQ:reply,id=1]'+text_bubbles('好'*30)[0],text_bubbles('&#[]'*20)[0],'[CQ:image,file=x]']
        out=limit_group_reply(segments,'group:'+G,CFG)
        self.assertEqual(len(out),2)
        self.assertTrue(out[0].startswith('[CQ:reply,id=1]'))
        self.assertEqual(sum(len(html.unescape(re.sub(r'\[CQ:[^\]]*\]','',s))) for s in out),40)
        self.assertEqual(limit_group_reply(segments,'group:100',CFG),segments)
        self.assertEqual(limit_group_reply(segments,'private:'+G,CFG),segments)
        self.assertEqual(limit_group_reply(['字'*50,'[CQ:image,file=x]','[CQ:image,file=y]'],'group:'+G,CFG),['字'*40,'[CQ:image,file=x]'])

    def test_both_providers_commit_limited_turn_and_prompt_keeps_persona(self):
        for provider in ('gemini','dsh'):
            with self.subTest(provider=provider),tempfile.TemporaryDirectory() as folder:
                base=Path(folder);(base/'config.json').write_text(json.dumps(CFG))
                (base/'group_profiles.json').write_text(json.dumps({'default_profile':'cat','profiles':{'cat':{'tone':'保持猫娘口吻','length':'旧长度'}}}))
                profile,_=resolve_profile('group:'+G,base)
                self.assertEqual(profile['tone'],'保持猫娘口吻');self.assertIn('40字',profile['length'])
                c=TurnCoordinator(base)
                if provider=='dsh':ProviderHealth(base).set_mode('dsh')
                c.publish({'session_key':'group:'+G,'trigger_id':'limit-'+provider})
                claim=c.claim_fallback() if provider=='dsh' else c.claim_primary()
                self.assertTrue(c.submit_reply(claim['token'],['字'*35,'啊'*30,'第三条'],provider)['accepted'])
                record=json.loads((base/'state/outbox.jsonl').read_text())
                self.assertEqual(record['segments'],['字'*35,'啊'*5])

class InitiativeTests(unittest.IsolatedAsyncioTestCase):
    loop_factory=OfflineEventLoop

    def test_probability_skips_whole_batch_before_model_and_at_is_exempt(self):
        for mode in ('上下文积累判断','即时唤醒(被@/私聊)'):
            engine=bridge.DynamicHakimiEngine();cfg=config();cfg.update(PROACTIVE_CFG)
            cfg['bot_profile']['random_peek_groups']=[G]
            engine.unread_counts['group:'+G]=1
            engine.context_windows['group:'+G]=[{'message_id':1,'user_id':'22','text':'怎么理解这句话','received_at':1000}]
            engine.conversation_scheduler.observe('group:'+G,'怎么理解这句话','22',1,1000,mentioned=mode.startswith('即时'))
            engine.trigger_queue.append({'session_key':'group:'+G,'mode':mode,'score':2,'reasons':[],'group_theme_info':{}})
            c=Mock();c.snapshot.return_value={'status':'idle'};c.publish.return_value={'accepted':True,'trigger_id':'test'}
            with tempfile.TemporaryDirectory() as folder,patch.object(bridge,'engine',engine),patch.object(bridge,'load_config',return_value=cfg),patch.object(bridge,'coordinator_for',return_value=c),patch.object(bridge.time,'time',return_value=1200),patch.object(bridge.random,'random',return_value=.99),patch.object(bridge,'PENDING_TRIGGER',Path(folder)/'pending.json'):
                bridge.flush_trigger_queue_if_waiting()
            self.assertEqual(c.publish.call_count,1 if mode.startswith('即时') else 0)
            self.assertFalse(engine.trigger_queue)
            self.assertFalse(engine.conversation_scheduler.group('group:'+G).pending)

if __name__=='__main__':unittest.main()
