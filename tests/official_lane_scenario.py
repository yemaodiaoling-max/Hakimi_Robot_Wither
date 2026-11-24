"""Subprocess end-to-end fixture. Never connects to QQ or a model provider."""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

RUNTIME = Path(sys.argv[1]).resolve()
os.environ.update(HAKIMI_ROOT=str(RUNTIME.parent), HAKIMI_BASE=str(RUNTIME))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import hakimi  # noqa: E402  (package code; the runtime folder holds data only)
from hakimi.bridge import bridge_daemon as bridge
from hakimi.models import fallback_runner
from hakimi.transport import official_lane
from hakimi.transport import official_transport
from PIL import Image
from hakimi.models.fallback_supervisor import coordinator_for
from hakimi.prompt.prompt_context import activate_context, render_prompt
from hakimi.tools import qq_cli
from types import SimpleNamespace

calls = []
call_paths = []
fallback_prompts = []
running_adapter = None
NO_GROUP_INFO = '--no-group-info' in sys.argv[2:]
COLD_ONLY = '--cold-topic' in sys.argv[2:]


class FakeAPI:
    def __init__(self, *_): pass
    async def call(self, method, path, data=None):
        if COLD_ONLY and method=='GET' and path.endswith('/bot_state'):
            return {'allow_proactive_msg':True}
        if method == 'GET' and path.endswith('/info'):
            if NO_GROUP_INFO:
                raise official_transport.OfficialError('HTTP_403')
            return {'group_name': '学习测试群'}
        assert path.endswith('/messages')
        calls.append(data)
        call_paths.append(path)
        return {"id": "BOT_" + str(len(calls)), "ext_info": {"ref_idx": "REFIDX_bot_" + str(len(calls))}}


async def mock_gateway(self, queue, status):
    global running_adapter
    running_adapter = self
    self.self_remote = 'BOT_SELF'
    status(True, None)
    if COLD_ONLY:
        await self.ensure_group('OFFLINE_TEST_GROUP')
        from hakimi.bridge.cold_topics import cold_store
        cold_store(str(RUNTIME)).bootstrap(bridge.load_config(),time.time()-75000)
    else:
        await emit(self, queue, 'SOURCE_PRIMARY', True)
    while True:
        await asyncio.sleep(30)


async def emit(adapter, queue, mid, mentioned, group='OFFLINE_TEST_GROUP', kind='GROUP_MESSAGE_CREATE'):
    data = {"id": mid, "group_openid": group, "content": "请解释一下这一步为什么成立？",
            "author": {"id": "MEMBER_A", "username": "离线测试群友", "bot": False},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message_scene": {"ext": ["msg_idx=REFIDX_" + mid]}}
    if mid == 'SOURCE_PRIMARY':
        data['attachments'] = [{'content_type':'image/png','url':'https://gchat.qpic.cn/offline-image'}]
    if mentioned:
        data['content'] = '<@GROUP_BOT_SELF> ' + data['content']
        data['mentions'] = [{'id':'GROUP_BOT_SELF', 'is_you':True}]
    event = await adapter.translate(kind, data)
    await queue.put(event)


async def until(predicate):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(.05)
    state=json.loads((RUNTIME/'state/delivery_state.json').read_text(encoding='utf-8'))
    turn=json.loads((RUNTIME/'state/turn_state.json').read_text(encoding='utf-8'))
    detail={'pending_session':state.get('pending',{}).get('session_key'),'last_event':state.get('last_event')}
    store=official_transport.RouteStore(RUNTIME/'state/official_routes.sqlite3')
    detail['send_error']=store.get('last_send_error')
    raise AssertionError('offline scenario timed out; sent_sources='+str([r.get('msg_id') for r in calls])
                         +'; fallback_calls='+str(len(fallback_prompts))+'; delivery='+str(detail)
                         +'; turn='+str({k:turn.get(k) for k in ('status','provider','outcome')}))


def fallback(prompt, _base, **_kwargs):
    fallback_prompts.append(prompt)
    assert '嘴硬心软' in prompt
    assert '猫系松弛' in prompt
    text = '备用端监听窗口验证' if '本轮是被@回复后的监听窗口' in prompt else '备用端离线验证答复'
    return {"action": "reply", "text": text}


async def scenario():
    cfg = json.loads((RUNTIME / 'config.json').read_text(encoding='utf-8'))
    cfg['conversation_scheduler']['mention_buffer_seconds'] = 0
    cfg['delivery_policy'].update(global_min_interval_seconds=.01, session_min_interval_seconds=.01,
                                  max_messages_per_minute=30)  # Synthetic bubbles complete immediately.
    cfg['fallback']['min_call_interval_seconds']=0  # Synthetic backends run immediately in this fixture only.
    cfg['fallback']['max_calls_per_hour']=8
    if COLD_ONLY:
        cfg['lurker_schedule']['proactive_topic']={'enable':True,'quiet_window_seconds':72000,'window_jitter_seconds':0}
    (RUNTIME / 'config.json').write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')
    lane_cfg=json.loads((RUNTIME/'official_lane_config.json').read_text(encoding='utf-8'))
    lane_cfg.update(operation_mode='normal',auto_adapt_groups=True)
    if COLD_ONLY:lane_cfg['allow_proactive']=True
    (RUNTIME/'official_lane_config.json').write_text(json.dumps(lane_cfg),encoding='utf-8')
    store = official_transport.RouteStore(RUNTIME / 'state/official_routes.sqlite3')
    store.set('group_openid', 'OFFLINE_TEST_GROUP')
    official_lane.OfficialAPI = FakeAPI
    def fake_image(url, destination):
        Image.new('RGB', (4,4), 'white').save(destination, 'JPEG')
        return str(destination)
    official_transport.download_image = fake_image
    official_transport.OfficialAdapter.gateway = mock_gateway
    fallback_runner.run_fallback = fallback
    os.environ['HAKIMI_OFFICIAL_APPSECRET'] = 'offline-placeholder-only'
    worker = asyncio.create_task(official_lane.main())
    coordinator = coordinator_for(RUNTIME, cfg)
    try:
        await until(lambda: coordinator.snapshot()['status'] == 'queued')
        first = coordinator.claim_primary()
        if COLD_ONLY:
            from hakimi.bridge.cold_topics import cold_store
            assert first['payload']['wake_mode']=='冷场主动抛话题'
            assert first['payload']['is_proactive'] is True
            assert store.source(first['trigger_id']) is None
            prompt=render_prompt(first['payload'],RUNTIME,first)
            assert 'cold_topic_request' in prompt
            activate_context(first['payload'],RUNTIME,claim=first)
            qq_cli.cmd_reply(SimpleNamespace(session='group:850245632',turn=first['token'],
                text='最近有哪道数学题把你们卡住了',reply_to='',sticker=''))
            await until(lambda:len(calls)==1)
            assert 'msg_id' not in calls[0], 'a cold opener must not reuse an expired passive source'
            controller=cold_store(str(RUNTIME))
            with controller.index.db() as db:
                assert db.execute('SELECT status FROM cold_attempts').fetchone()[0]=='watching'
            incoming={'id':'COLD_RESPONSE','group_openid':'OFFLINE_TEST_GROUP','content':'我还卡在三角函数那一步',
                'author':{'id':'MEMBER_A','username':'离线成员','bot':False},
                'timestamp':datetime.now(timezone.utc).isoformat(),
                'message_scene':{'ext':['msg_idx=REFIDX_response','ref_msg_idx=REFIDX_bot_1']}}
            event=await running_adapter.translate('GROUP_MESSAGE_CREATE',incoming)
            await bridge.INCOMING_EVENTS.put(event)
            await until(lambda:(RUNTIME/'state/session_memory.jsonl').exists() and
                        '三角函数' in (RUNTIME/'state/session_memory.jsonl').read_text(encoding='utf-8'))
            assert bridge.engine.conversation_scheduler.group('group:850245632').listen_round==0
            print('OFFICIAL_COLD_TOPIC_NO_RECENT_SOURCE_OK')
            return
        # Verify source-age TTL, plus an actual primary tool entry and reply commit.
        assert first['deadline'] <= first['payload']['created_at'] + 130.1
        prompt = render_prompt(first['payload'], RUNTIME, first)
        assert '哈基米' in prompt and 'selected_real_dialogues' in prompt
        assert 'OFFLINE_TEST_GROUP' not in prompt and 'offline-placeholder-only' not in prompt
        context = activate_context(first['payload'], RUNTIME, claim=first)
        assert len(context['allowed_image_paths']) == 1, 'official cached image must enter the trusted lease'
        image_path = context['allowed_image_paths'][0]
        assert Path(image_path).name.startswith('img_group_850245632_')
        qq_cli.cmd_save_sticker(SimpleNamespace(session='group:850245632', turn=first['token'],
            url=image_path, name='official_collection_check.jpg'))
        assert (RUNTIME / 'stickers/collected/official_collection_check.jpg').is_file()
        qq_cli.cmd_tool(SimpleNamespace(session='group:850245632', turn=first['token'], name='calculator', input='{"expression":"2+3"}'))
        qq_cli.cmd_reply(SimpleNamespace(session='group:850245632', turn=first['token'],
            text='离线验证第一条|离线验证第二条|第三条|第四条|第五条|第六条', reply_to='', sticker=''))
        await until(lambda: len(calls) == 5)
        assert all(row['msg_id'] == 'SOURCE_PRIMARY' for row in calls)
        assert [row['msg_seq'] for row in calls] == [1, 2, 3, 4, 5]
        assert '第六条' in ''.join(row['content'] for row in calls)
        assert all('message_reference' not in row for row in calls)
        assert bridge.engine.conversation_scheduler.group('group:850245632').last_reply > 0
        await emit(running_adapter, bridge.INCOMING_EVENTS, 'SOURCE_NEW_PRIMARY', True, 'OFFLINE_SECOND_GROUP','GROUP_AT_MESSAGE_CREATE')
        await until(lambda: coordinator.snapshot()['status']=='queued')
        new_group=coordinator.claim_primary()
        new_session=new_group['payload']['session_key']
        assert new_session!='group:850245632'
        context=activate_context(new_group['payload'], RUNTIME, claim=new_group)
        from hakimi.prompt.prompt_context import resolve_profile
        assert resolve_profile(new_session,RUNTIME)[0]['name']==('quiet_default' if NO_GROUP_INFO else 'study_calm')
        assert all(row['session_key']==new_session for row in new_group['payload']['recent_context_messages'])
        qq_cli.cmd_reply(SimpleNamespace(session=new_session,turn=new_group['token'],text='新群主模型离线验证',reply_to='',sticker=''))
        await until(lambda:len(calls)==6)
        assert calls[-1]['msg_id']=='SOURCE_NEW_PRIMARY'
        assert call_paths[-1]=='/v2/groups/OFFLINE_SECOND_GROUP/messages'
        await until(lambda:bridge.engine.conversation_scheduler.group(new_session).listen_due>0)
        listening=bridge.engine.conversation_scheduler.group(new_session)
        assert 14 <= listening.listen_due-time.time() <= 15.1
        await emit(running_adapter,bridge.INCOMING_EVENTS,'SOURCE_FOLLOWUP',False,'OFFLINE_SECOND_GROUP')
        await until(lambda:bool(bridge.engine.conversation_scheduler.group(new_session).pending))
        assert coordinator.snapshot()['status']=='completed', 'wait for the listening window before dispatch'
        # Advance the deadline only in this fixture, without a real 15-second wait.
        listening.listen_due=time.time()-.01
        bridge.engine.study_schedule_event.set()
        await until(lambda:coordinator.snapshot()['status']=='queued')
        following=coordinator.claim_primary()
        assert following['payload']['wake_mode']=='被@后监听窗口'
        assert following['payload']['listening_window']['round']==1
        assert len(following['payload']['listening_window']['new_message_ids'])==1
        activate_context(following['payload'],RUNTIME,claim=following)
        qq_cli.cmd_reply(SimpleNamespace(session=new_session,turn=following['token'],text='未被at的相关续聊验证',reply_to='',sticker=''))
        await until(lambda:len(calls)==7)
        await until(lambda:listening.listen_due>0)
        assert 29 <= listening.listen_due-time.time() <= 30.1
        assert calls[-1]['msg_id']=='SOURCE_FOLLOWUP'
        adapter = official_transport.OfficialAdapter(RUNTIME, {'test_group_number':'850245632'}, FakeAPI(), store)
        await emit(adapter, bridge.INCOMING_EVENTS, 'SOURCE_FALLBACK', True)
        await until(lambda: coordinator.snapshot()['status'] == 'queued')
        second = coordinator.claim_primary()
        assert coordinator.report_failure(second['token'], 'quota')
        await until(lambda: len(calls) == 8)
        assert calls[-1]['msg_id'] == 'SOURCE_FALLBACK'
        assert calls[-1]['content'] == '备用端离线验证答复'
        assert len(fallback_prompts) == 1
        assert coordinator.snapshot()['provider'] == 'dsh'
        assert not coordinator.submit_reply(second['token'], ['迟到主模型'], 'gemini')['accepted']
        await emit(running_adapter, bridge.INCOMING_EVENTS, 'SOURCE_NEW_FALLBACK', True, 'OFFLINE_SECOND_GROUP')
        await until(lambda: coordinator.snapshot()['status']=='queued')
        last=coordinator.claim_primary()
        assert last['payload']['session_key']==new_session
        assert coordinator.report_failure(last['token'],'quota')
        await until(lambda:len(calls)==9)
        assert calls[-1]['msg_id']=='SOURCE_NEW_FALLBACK'
        assert call_paths[-1]=='/v2/groups/OFFLINE_SECOND_GROUP/messages'
        assert len(fallback_prompts)==2
        await until(lambda:listening.listen_due>0)
        await emit(running_adapter,bridge.INCOMING_EVENTS,'SOURCE_NEW_FOLLOWUP_DSH',False,'OFFLINE_SECOND_GROUP')
        await until(lambda:bool(listening.pending))
        listening.listen_due=time.time()-.01
        bridge.engine.study_schedule_event.set()
        await until(lambda:coordinator.snapshot()['status']=='queued')
        dsh_followup=coordinator.claim_primary()
        assert dsh_followup['payload']['wake_mode']=='被@后监听窗口'
        assert coordinator.report_failure(dsh_followup['token'],'quota')
        await until(lambda:len(calls)==10)
        assert calls[-1]['msg_id']=='SOURCE_NEW_FOLLOWUP_DSH'
        assert len(fallback_prompts)==3
        assert '本轮是被@回复后的监听窗口' in fallback_prompts[-1]
        assert '决定接话或silent' in fallback_prompts[-1]
        await until(lambda:listening.listen_due>0)
        assert 29 <= listening.listen_due-time.time() <= 30.1
        # A suppressed duplicate was never actually spoken: end listening, rather
        # than waiting forever for a send acknowledgement that cannot arrive.
        await emit(running_adapter,bridge.INCOMING_EVENTS,'SOURCE_DUPLICATE_DSH',False,'OFFLINE_SECOND_GROUP')
        await until(lambda:bool(listening.pending))
        listening.listen_due=time.time()-.01
        bridge.engine.study_schedule_event.set()
        await until(lambda:coordinator.snapshot()['status']=='queued')
        duplicate=coordinator.claim_primary()
        assert coordinator.report_failure(duplicate['token'],'quota')
        await until(lambda:len(fallback_prompts)==4 and not listening.listen_round)
        assert len(calls)==10
        assert not store.get('member_mapping')
        print('OFFICIAL_OFFLINE_END_TO_END_OK', flush=True)
        print('OFFICIAL_MULTIGROUP_PRIMARY_AND_DSH_OK',flush=True)
        print('OFFICIAL_UNMENTIONED_CONTINUATION_OK',flush=True)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


if __name__ == '__main__':
    from test_delivery_guard import OfflineEventLoop
    asyncio.run(scenario(), loop_factory=OfflineEventLoop)


