"""Availability fallback: explicit errors or deadlines, never guessed billing state."""
from hakimi import paths
import asyncio
import json
import os
import time

from hakimi.prompt.prompt_context import build_context
from hakimi.bridge.turn_coordinator import TurnCoordinator
from hakimi.bridge.user_reply_quota import is_private_session
from hakimi.models.fallback_runner import FallbackError
from hakimi.models.fallback_media import prepare_images
from hakimi.features.codex_help import HelpQueue, question_brief, report_fault, worker_healthy, settings as help_settings
from hakimi.models.provider_health import availability
from hakimi.tools import bilibili_video
from hakimi.models.fallback_prompt import BASE_DIR, _fallback_text_segments, _save_status, fallback_prompt, fallback_segments  # noqa: F401 (re-export)
from hakimi.models.fallback_commit import submit_fallback_result  # noqa: F401 (re-export)

POLICY_REVISION = "2026-10-27-dsh-tools-math-v1"


def _limited_calls(status):
    # Old checkpoints recorded only timestamps, without the conversation type.
    if 'limited_calls' not in status:
        status['limited_calls'] = list(status.get('calls', []))
    return status['limited_calls']


def can_call(status, settings, claim):
    return (is_private_session(claim['payload'].get('session_key'))
            or len(_limited_calls(status)) < settings.get('max_calls_per_hour', 20))


def record_call(status, claim, now):
    limited = _limited_calls(status)
    status['calls'].append(now)
    if not is_private_session(claim['payload'].get('session_key')):
        limited.append(now)


def coordinator_for(base, cfg):
    settings = cfg.get("fallback", {})
    return TurnCoordinator(base,
                           grace_seconds=settings.get("primary_grace_seconds", 90),
                           primary_timeout_seconds=settings.get("primary_timeout_seconds", 180),
                           confirmation_seconds=settings.get("confirmation_seconds", 30),
                           primary_max_seconds=settings.get("primary_max_seconds", 420),
                           fallback_timeout_seconds=settings.get("fallback_timeout_seconds", 120),
                           ttl_seconds=600)


async def resolve_consultation(result, claim, coordinator, base, generate, status, fallback_settings):
    draft = {"action": "reply", "text": result["text"]}
    cfg = help_settings(base)
    remaining = claim["deadline"]-time.time()
    if (not cfg["enabled"] or not worker_healthy(base) or remaining < 38
            or not can_call(status, fallback_settings, claim)):
        return draft
    queue = HelpQueue(base)
    until = min(time.time()+cfg["wait_seconds"], claim["deadline"]-32)
    request = queue.enqueue(question_brief(claim["payload"], result["question"], base), deadline=until)
    if not request["accepted"]:
        return draft
    status["consultation_id"] = request["id"]
    _save_status(base, status)
    advice = None
    while time.time() < until:
        if not coordinator.check_token(claim["token"], "dsh"):
            return {"action": "silent", "text": ""}
        row = queue.get(request["id"])
        if row["state"] == "completed":
            advice = row["result"]
            break
        if row["state"] in {"failed", "expired"}:
            break
        await asyncio.sleep(.8)
    if not advice or not coordinator.check_token(claim["token"], "dsh"):
        return draft
    # A second DSH generation is charged to the same ordinary DSH hourly budget.
    record_call(status, claim, time.time())
    _save_status(base, status)
    try:
        composed = await generate(fallback_prompt(claim["payload"], base, consultation_result=advice,
                                                images=claim.get("_images", ())), claim, 30)
        return composed if composed["action"] in {"reply", "silent"} else draft
    except Exception as exc:
        report_fault(base, "dsh_composition", exc)
        return draft


async def fallback_loop(load_config, base=BASE_DIR, runner=None):
    async def invoke(prompt, claim, ceiling, fb_cfg=None):
        if runner is not None:
            return await asyncio.to_thread(runner, prompt, base)
        limit = max(1, min(ceiling, claim["deadline"]-time.time()-2))
        if fb_cfg and fb_cfg.get("backend") == "http_api":
            from hakimi.models.fallback_http import run_http_fallback
            return await asyncio.to_thread(run_http_fallback, prompt, base, timeout_seconds=limit,
                                           images=claim.get("_images", ()), cfg=fb_cfg)
        from hakimi.models.fallback_runner import run_fallback
        return await asyncio.to_thread(run_fallback, prompt, base, timeout_seconds=limit,
                                       images=claim.get("_images", ()))

    async def generate(prompt, claim, ceiling=90, fb_cfg=None):
        try:
            return await invoke(prompt, claim, ceiling, fb_cfg=fb_cfg)
        except FallbackError as exc:
            hint = getattr(exc, 'protocol_hint', None)
            if (not hint or claim.get('_protocol_repaired') or
                    not coordinator.check_token(claim['token'], 'dsh') or
                    claim['deadline']-time.time() < 15 or
                    not can_call(status, settings, claim)):
                raise
            claim['_protocol_repaired'] = True
            record_call(status, claim, time.time())
            status['protocol_repairs'] = status.get('protocol_repairs',0)+1
            # Fixed diagnostic wording only: no response values or chat text.
            status['last_protocol_hint'] = hint
            _save_status(base, status)
            from hakimi.tools.math_render import log_tool_call
            log_tool_call(base, 'protocol', 'repair', provider='dsh', hint=hint)
            from hakimi.models.fallback_prompt import append_fallback_context
            suffix=('\n上次输出未通过格式校验。具体问题：'+hint+
                '\n请根据原问题重新生成一个完整 JSON 对象。普通回复示例：{"action":"reply","text":"回复正文"}。'
                '仅在确实需要调用工具时使用 action=tool、tool、input、text。')
            return await invoke(append_fallback_context(prompt,suffix), claim, ceiling, fb_cfg=fb_cfg)
    state_path = base / "state" / "fallback_status.json"
    try:
        status = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    status.setdefault("calls", [])
    status.setdefault("consecutive_errors", 0)
    status.setdefault("paused_until", 0)
    status.update(policy_revision=POLICY_REVISION, worker_pid=os.getpid(), loaded_at=time.time(),
                  sticker_mode="existing_only_no_collection", clock_timezone_labels=True,
                  vision_mode="current_session_pixels_max_2")
    _save_status(base, status)
    while True:
        try:
            cfg = load_config()
            settings = cfg.get("fallback", {})
            now = time.time()
            status["calls"] = [at for at in status.get("calls", []) if isinstance(at, (int, float)) and now - at < 3600]
            from hakimi.models import aux_config
            fb_cfg = aux_config.fallback_config(base, settings)
            enabled = fb_cfg.get("enabled", False)
            limited = [at for at in _limited_calls(status) if isinstance(at, (int, float)) and now - at < 3600]
            status['limited_calls'] = limited
            budget = len(limited) < settings.get("max_calls_per_hour", 20)
            interval = settings.get("min_call_interval_seconds",30)
            health = availability(base)
            if health["fast"]:interval=min(interval,5)
            paced = not limited or now - limited[-1] >= interval
            if enabled and now >= status.get("paused_until", 0):
                coordinator = coordinator_for(base, cfg)
                claim = None
                for lane in coordinator.lane_ids():
                    candidate = coordinator.for_lane(lane)
                    claim = candidate.claim_fallback() if budget and paced else candidate.claim_fallback(private_only=True)
                    if claim:
                        coordinator = candidate
                        break
                if claim:
                    from hakimi.bridge.turn_lanes import lane_context
                    with lane_context(coordinator.lane_id):
                        coordinator.enable_post_reply(claim["token"])
                        record_call(status, claim, now)
                        status.update(last_started_at=now, last_trigger_id=claim["trigger_id"], state="running")
                        _save_status(base, status)
                        try:
                            from hakimi.web.search_routing import prepare as prepare_search
                            claim['payload']['_web_context'] = await asyncio.to_thread(prepare_search,claim['payload'],base,timeout=min(12,max(.1,claim['deadline']-time.time()-10)))
                            cold=bool(claim['payload'].get('cold_topic_request'))
                            video = None if cold else await asyncio.to_thread(bilibili_video.prepare, claim["payload"], base)
                            if video:
                                claim["payload"]["_video"] = video
                            claim["_images"] = () if cold else (bilibili_video.images(video, claim["payload"], base) if video and not video.get("error")
                                                else await asyncio.to_thread(prepare_images, claim["payload"], base))
                            status["last_image_count"] = len(claim["_images"])
                            can_consult = (help_settings(base)["enabled"] and worker_healthy(base)
                                           and can_call(status, settings, claim))
                            result = await generate(fallback_prompt(claim["payload"], base, allow_consult=can_consult,
                                                                   images=claim["_images"]), claim, fb_cfg=fb_cfg)
                            if result["action"] == "consult":
                                result = await resolve_consultation(result, claim, coordinator, base, generate, status, settings)
                            if result['action']=='tool':
                                from hakimi.models.dsh_tools import resolve_tools
                                result=await resolve_tools(result,claim,coordinator,base,generate,status,settings)
                            submission = await asyncio.to_thread(submit_fallback_result, result, claim, coordinator, base, finish=True)
                            accepted = submission['accepted']
                            status.update(state="completed" if accepted else "late_result_discarded",
                                          consecutive_errors=0, last_finished_at=time.time())
                        except Exception as exc:
                            if getattr(coordinator, 'was_superseded', lambda _: False)(claim.get('trigger_id')):
                                status.update(state='superseded', consecutive_errors=0, last_finished_at=time.time())
                                _save_status(base, status)
                                continue
                            from hakimi.bridge.message_lifecycle import record
                            code=str(exc) if isinstance(exc,FallbackError) else type(exc).__name__
                            record(base,claim['payload'],'generation_error',trigger_id=claim['trigger_id'],reason=code)
                            report_fault(base, "dsh_fallback", exc)
                            coordinator.complete(claim["token"], outcome="fallback_error")
                            count = status.get("consecutive_errors", 0) + 1
                            status.update(state="failed", consecutive_errors=count,
                                          last_error_type=type(exc).__name__, last_finished_at=time.time())
                            if isinstance(exc, FallbackError) and str(exc).startswith("FALLBACK_"):
                                status["last_error_code"] = str(exc)
                            if count >= 3:
                                status["paused_until"] = time.time() + 600
                            # Never log model output, credentials or child stderr into the public prompt.
                            print(f"[dsh备用失败] {type(exc).__name__}；本任务不自动重试")
                        _save_status(base, status)
        except Exception as exc:
            report_fault(base, "fallback_monitor", exc)
            print(f"[备用监控异常] {type(exc).__name__}")
        await asyncio.sleep(1)

