"""Deliver completed receipts on the ordinary bridge loop, never an ASR thread."""
import asyncio,re,time
from hakimi.memory import voice_receipts as store

async def deliver(base,process_event):
    while True:
        try:
            job=await asyncio.to_thread(store.ready,base)
            if not job:await asyncio.sleep(.2);continue
            state=store.source_status(base,job['session'],job['mid'])
            if state=='removed':store.discard(base,job['session'],job['mid'],'SOURCE_FORGOTTEN');continue
            if state=='active':store.acknowledge(base,job['session'],job['mid']);continue
            event=dict(job['event']);event.pop('_voice_attachments',None)
            event.update(_voice_done=True,_voice_received_at=job['at'])
            should_dispatch=store.dispatch(base,job['session'],job['mid'])
            if job['error']:
                # Preserve genuine typed content accompanying an unsuccessful recording.
                typed=re.sub(r'\[CQ:[^\]]*\]','',str(event.get('raw_message',''))).strip()
                if typed and should_dispatch:await process_event(event)
                store.acknowledge(base,job['session'],job['mid']);continue
            event.update(_voice_ref=job['mid'],_voice_status='transcribed',_voice_semantic=store.excerpt(job['text']))
            if should_dispatch:
                handled=await process_event(event)
                if handled and handled.get('voice_deferred'):
                    with store.database(base) as db:db.execute("UPDATE jobs SET dispatched=0 WHERE session=? AND mid=? AND status='ready'",(job['session'],job['mid']))
                    await asyncio.sleep(1);continue
                if not handled:store.discard(base,job['session'],job['mid'],'ADMISSION_CHANGED');continue
            if not store.live(base,job['session'],job['mid']):
                # Recovery after a crash/temporary memory failure archives the one
                # receipt without replaying uncertain scheduler/wake side effects.
                from hakimi.memory.memory_store import store as memory_store
                row={'session_key':job['session'],'message_id':job['mid'],'user_id':str(event['user_id']),'received_at':job['at'],'text':'[语音消息，自动转写原文仅本会话可读，voice.read message_id='+job['mid']+']','channel':'voice'}
                await asyncio.to_thread(memory_store(str(base)).observe,row)
            if store.live(base,job['session'],job['mid']):store.acknowledge(base,job['session'],job['mid'])
            else:await asyncio.sleep(1)
        except Exception as exc:
            from hakimi.features.codex_help import report_fault
            report_fault(base,'voice_delivery',exc)
            await asyncio.sleep(1)
