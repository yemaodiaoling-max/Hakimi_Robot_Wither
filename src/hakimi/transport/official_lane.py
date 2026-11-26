"""Entrypoint copied into the official runtime: reuse the existing bridge engine."""
from hakimi import paths
import asyncio
import json
import os
from pathlib import Path
import sys
import time

from hakimi.transport.official_api import OfficialAPI; from hakimi.transport.official_transport import OfficialAdapter; from hakimi.chat.official_base import OfficialError, fixed_json; from hakimi.transport.official_routes import RouteStore


async def main():
    sys.stdout.reconfigure(encoding="utf-8")
    base = paths.base()
    if base.name != "official_test_runtime":
        raise OfficialError("ISOLATED_RUNTIME_REQUIRED")
    from hakimi.bridge import bridge_daemon as bridge
    from hakimi.models.fallback_supervisor import coordinator_for, fallback_loop
    from hakimi.bridge.turn_coordinator import TurnCoordinator
    from hakimi.bridge.delivery_guard import DeliveryGuard
    cfg = json.loads((base / "official_lane_config.json").read_text(encoding="utf-8"))
    secret = os.environ.pop("HAKIMI_OFFICIAL_APPSECRET", "")
    if not secret:
        raise OfficialError("CREDENTIALS_REQUIRED")
    store = RouteStore(base / "state/official_routes.sqlite3", persistent=True)
    api = OfficialAPI(cfg["app_id"], secret)
    adapter = OfficialAdapter(base, cfg, api, store)

    class SourceCoordinator(TurnCoordinator):
        def publish(self, payload, *, replace_trigger_id=None):
            messages = payload.get("recent_context_messages", [])
            route = adapter.route_for_session(payload.get("session_key"))
            if not route:
                return {"accepted": False, "reason": "official_group_not_active"}
            explicit = bool(payload.get('pending_explicit_question_ids')) or '即时唤醒' in str(payload.get('wake_mode', ''))
            if route.get('kind') != 'private' and not explicit and not payload.get('internal_review'):
                from hakimi.transport.group_capabilities import can_autonomous
                if not can_autonomous(base, payload.get('session_key')):
                    return {'accepted': False, 'reason': 'official_proactive_disabled'}
            if payload.get('wake_mode')=='冷场主动抛话题':
                if route.get('kind')=='private' or cfg.get('allow_proactive') is not True:
                    return {'accepted':False,'reason':'official_proactive_disabled'}
                self.ttl_seconds=600
                return super().publish(dict(payload,created_at=time.time(),is_proactive=True))
            # Prefer the explicit question; otherwise latest same-session human input.
            directed = {str(mid) for mid in payload.get("pending_explicit_question_ids", [])}
            eligible = [store.message(str(row.get("message_id"))) for row in messages
                        if isinstance(row, dict) and row.get("user_id") != "SELF"
                        and row.get("session_key") == payload.get("session_key")
                        and (not directed or str(row.get("message_id")) in directed)]
            eligible = [row for row in eligible if row and row["direction"] == "in" and row["group_openid"] == route.get("scope",route["openid"])]
            if not eligible:
                return {"accepted": False, "reason": "no_official_reply_source"}
            # QQ timestamps can tie. Keep the last clarification as the reply reference.
            target = str(payload.get("reply_target_message_id", ""))
            source = next((row for row in eligible if directed and str(row["local_id"]) == target), None)
            if source is None:
                if replace_trigger_id is not None:
                    return {"accepted": False, "reason": "replacement_unavailable"}
                source = max(eligible, key=lambda row: row["at"])
            # Reserve up to 75s for five paced bubbles inside the 300s window.
            self.ttl_seconds = 600 if cfg.get("allow_proactive") is True and route.get("kind")!="private" else 225
            copied = dict(payload, created_at=source["at"])
            result = super().publish(copied, replace_trigger_id=replace_trigger_id)
            if result["accepted"]:
                store.attach_turn(result["trigger_id"], source["local_id"])
            return result

    def official_coordinator(root, config):
        normal = coordinator_for(root, config)
        return SourceCoordinator(root, grace_seconds=normal.grace_seconds,
            primary_timeout_seconds=normal.primary_timeout_seconds,
            primary_max_seconds=normal.primary_max_seconds, confirmation_seconds=normal.confirmation_seconds,
            fallback_timeout_seconds=normal.fallback_timeout_seconds,
            ttl_seconds=600 if cfg.get("allow_proactive") is True else 225)

    bridge.coordinator_for = official_coordinator
    bridge.send_via_onebot = adapter.send
    async def can_start_cold_topic(session):
        route=adapter.route_for_session(session)
        return bool(route and route.get('kind')!='private' and await adapter.can_send_plain(route['openid'],'cold-check:'+str(int(time.time()//600))))
    bridge.cold_topic_allowed=can_start_cold_topic
    async def permission_loop():
        while True:
            try:
                await adapter.refresh_group_permissions()
            except Exception as exc:
                print(json.dumps({'component': 'group_permissions', 'error': type(exc).__name__}), flush=True)
            await asyncio.sleep(30)
    # Official images are already downloaded/validated by the adapter. Never call NapCat.
    def cached_image(file_param, url_param, tag):
        path = Path(file_param)
        root = base / "state/img_cache"
        return path.as_posix() if path.is_file() and path.resolve().is_relative_to(root.resolve()) else None
    bridge.cache_qq_image_to_jpg = cached_image
    bridge.onebot_http_post = lambda *args, **kwargs: {}  # No cross-transport history fallback.
    DeliveryGuard(bridge.DELIVERY_STATE_FILE, bridge.OUTBOX_FILE, bridge.load_config().get("delivery_policy", {}))
    bridge.restore_directed_turns(official_coordinator(base, bridge.load_config()))
    bridge.engine.conversation_scheduler.restore(base, time.time())
    bridge.engine.restore_listening_unread()
    bridge.engine.restore_recent_study_unread(bridge.load_config(), base=base)
    online = False
    last_error = None

    def status(connected, error):
        nonlocal online, last_error
        if connected != online or error and error != last_error:
            try:
                with (base / "state/official_connection_events.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"at": time.time(), "online": connected, "error": error,
                                             "pid": os.getpid()})+"\n")
            except OSError:
                pass
        online = connected
        if error or connected:
            last_error = error
        if connected:
            bridge.ACTIVE_WS_CLIENTS["official"] = time.monotonic()
        else:
            bridge.ACTIVE_WS_CLIENTS.clear()

    async def heartbeat():
        while True:
            fixed_json(base / "state/official_worker.json", {
                "at": time.time(), "online": online, "bound": any(r.get("active",True) for r in (*adapter.routes.values(),*adapter.private_routes.values())),
                "groups": len(adapter.routes), "private_sessions":len(adapter.private_routes),
                "last_private_received_at":store.get("last_private_received_at"),
                "test_group": cfg["test_group_number"], "last_error": last_error, "pid": os.getpid()})
            await asyncio.sleep(5)

    try:
        async with asyncio.TaskGroup() as tasks:
            from hakimi.voice.delivery import deliver as deliver_voice
            for coro in (deliver_voice(base,bridge.process_incoming_event),adapter.gateway(bridge.INCOMING_EVENTS, status), heartbeat(), bridge.incoming_event_loop(),
                         bridge.watch_simulation_and_outbox(), bridge.random_window_peek_loop(),
                         bridge.auto_cleanup_loop(), bridge.relationship_review_loop(),
                         fallback_loop(bridge.load_config, base), permission_loop()):
                tasks.create_task(coro)
    finally:
        status(False, None)
        fixed_json(base / "state/official_worker.json", {"at": time.time(), "online": False,
            "bound": any(r.get("active",True) for r in (*adapter.routes.values(),*adapter.private_routes.values())), "test_group": cfg["test_group_number"],
            "last_error": last_error, "pid": os.getpid()})
        store.close()


def configuration_failure(exc):
    if isinstance(exc, BaseExceptionGroup):
        return any(configuration_failure(child) for child in exc.exceptions)
    return isinstance(exc, OfficialError) and (exc.code.startswith("GATEWAY_REQUIRES_CONFIGURATION_")
        or exc.code in {"CREDENTIALS_REQUIRED", "ISOLATED_RUNTIME_REQUIRED", "INVALID_GATEWAY"})


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (Exception, KeyboardInterrupt) as exc:
        if not isinstance(exc, KeyboardInterrupt):
            from hakimi.features.codex_help import report_fault
            report_fault(paths.base(), "official_worker", exc, code="process_crash")
        from hakimi.features.codex_help import exception_details
        print(json.dumps({"state": "stopped", "at": time.time(), **exception_details(exc)}))
        raise SystemExit(78 if configuration_failure(exc) else 2)
