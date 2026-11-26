"""Prepare/control an isolated official QQ test lane; never reconfigure NapCat."""
from hakimi import paths
import argparse
import asyncio
import base64
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from hakimi.chat.official_base import OfficialError, SELF_ALIAS, fixed_json; from hakimi.transport.official_routes import RouteStore

BASE = paths.root()
RUNTIME_NAME = "official_test_runtime"
CORE = ("genshin_version.py", "prompt_language.py", "model_trial.py", "bridge_daemon.py", "bot_tools.py", "conversation_scheduler.py", "delivery_guard.py",
        "fallback_runner.py", "fallback_supervisor.py", "fallback_style.py", "codex_help.py", "dsh_fallback_guard.mjs", "image_permissions.py",
        "participation_policy.py", "prompt_context.py", "memory_recall.py", "qq_cli.py", "quote_policy.py", "reply_format.py",
        "sticker_catalog.py", "sticker_policy.py", "study_recovery.py", "time_tool.py", "turn_coordinator.py",
        "group_reply_quota.py", "dsh_math.py", "member_mentions.py", "bot_registry.py", "impression_events.py",
        "official_transport.py", "official_lane.py", "official_groups.py", "runtime_io.py", "provider_health.py", "prompt_compaction.py", "prompt_budget.py", "memory_focus.py", "memory_usage.py",
        "fallback_media.py", "context_window.py", "forward_messages.py", "bilibili_video.py", "bilibili_login.py", "smart_memory.py", "tool_catalog.py", "file_delivery.py", "official_files.py", "feeding.py", "memory_store.py", "memory_lifecycle.py", "memory_graph.py", "memory_embedding_client.py", "memory_embedding_service.py", "memory_embedding_eval.py", "memory_graph_view.py", "memory_social.py", "memory_console.py", "identity_links.py", "legacy_md_notes.py", "identity_member_context.py", "cold_topics.py", "math_render.py", "host_secrets.py", "interaction_policy.py", "diary_store.py", "diary_latex.py", "dsh_tools.py",
        "personal_review.py", "terra_budget.py", "social_behavior.py", "affinity_curve.py", "affinity_policy.py",
        "vertex_route.py", "vertex_session.py", "vertex_web.py", "image_initiative.py")
RULES = ("style_guide.md", "style_examples.jsonl", "dsh_style_examples.jsonl")
SUPPORT = ("social_seed.json", "social_behavior_policy.json")
STATIC = ("memory_console_static/index.html", "memory_console_static/app.js", "memory_console_static/layout.js", "memory_console_static/styles.css",
          "memory_console_static/identity.html", "memory_console_static/identity.js", "memory_console_static/identity.css", "memory_console_static/identity-member-context.js",
          "memory_console_static/vertex-budget.js", "memory_console_static/vertex-budget.css")


def settings(base=BASE):
    path = base / "official_test_config.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if (value.get("version") != 1 or not re.fullmatch(r"[1-9][0-9]{4,19}", value.get("test_group_number", ""))
            or not re.fullmatch(r"[1-9][0-9]{4,19}", value.get("app_id", ""))
            or value.get("operation_mode", "test") not in {"test", "normal"}):
        raise OfficialError("INVALID_OFFICIAL_CONFIGURATION")
    return value


def runtime_path(base=BASE):
    path = base / RUNTIME_NAME
    if path.is_symlink() or path.resolve() != path.absolute():
        raise OfficialError("REDIRECTED_OFFICIAL_RUNTIME")
    return path


def prepare(base=BASE):
    cfg = settings(base)
    runtime = runtime_path(base)
    runtime.mkdir(exist_ok=True)
    state = runtime / "state"
    state.mkdir(exist_ok=True)
    if state.is_symlink() or state.resolve() != state.absolute():
        raise OfficialError("REDIRECTED_OFFICIAL_STATE")
    # Do not replace code underneath a running model/bridge; restart test lane explicitly.
    marker = state / "official_worker.json"
    if marker.exists() and time.time() - json.loads(marker.read_text(encoding="utf-8")).get("at", 0) < 20:
        raise OfficialError("OFFICIAL_LANE_ALREADY_RUNNING")
    # Validate account identity BEFORE replacing any runtime files. A placeholder
    # in a never-connected, empty test lane is safe to correct on first setup.
    store = RouteStore(state / "official_routes.sqlite3")
    store.prepare_identity(cfg["app_id"], cfg["test_group_number"])
    # Code (CORE) and console assets (STATIC) run from the installed package and
    # are no longer copied.  Persona rules and support data are instance files:
    # an operator copy in ``base`` wins, otherwise the packaged one is used.
    manifest = {}
    for name in RULES:
        data = paths.persona(name, base).read_bytes()
        (runtime / name).write_bytes(data)
        manifest[name] = hashlib.sha256(data).hexdigest()
    for name in SUPPORT:
        source = base / name
        if not source.is_file():
            continue
        data = source.read_bytes()
        (runtime / name).write_bytes(data)
        manifest[name] = hashlib.sha256(data).hexdigest()
    existing = json.loads((base / "config.json").read_text(encoding="utf-8"))
    group = cfg["test_group_number"]
    existing["official_lane"] = True
    existing["official_operation_mode"] = cfg.get("operation_mode", "test")
    existing["bot_profile"].update(self_qq=int(SELF_ALIAS), active_groups=[group], ignored_groups=[],
        allow_private_chat=cfg.get("allow_private_chat",False), ignored_user_ids=[], random_peek_groups=[group], low_frequency_groups=[group])
    existing["group_weights"] = {group: existing.get("group_weights", {}).get(group, {"weight": 1, "min_score": .2})}
    existing["user_bias"] = {"default_affinity": existing.get("user_bias", {}).get("default_affinity", 50), "users": {}}
    existing["onebot"] = {"ws_reverse_host": "127.0.0.1", "ws_reverse_port": 18081,
                          "http_api_url": "http://127.0.0.1:1"}
    if cfg.get("operation_mode") != "normal":
        existing["fallback"].update(fallback_timeout_seconds=95, max_calls_per_hour=3)
    elif not cfg.get("allow_proactive"):
        # Keep enough primary time inside the passive-only platform window.
        existing["fallback"]["fallback_timeout_seconds"] = 95
    existing.setdefault("delivery_policy", {}).update(max_segments_per_reply=5, max_queue_age_seconds=285)
    # The platform reply window is 300s; source receipt time is applied in official_lane.
    existing.setdefault("conversation_scheduler", {})["busy_group_sensitivity"] = {
        group: existing.get("conversation_scheduler", {}).get("busy_group_sensitivity", {}).get(group, 1)}
    fixed_json(runtime / "config.json", existing)
    profiles = json.loads((base / "group_profiles.json").read_text(encoding="utf-8"))
    profiles["groups"] = {group: profiles.get("groups", {}).get(group, {"profile": profiles["default_profile"]})}
    fixed_json(runtime / "group_profiles.json", profiles)
    sticker_root = runtime / "stickers"
    sticker_root.mkdir(exist_ok=True)
    for path in (base / "stickers").rglob("*"):
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".json"}:
            target = sticker_root / path.relative_to(base / "stickers")
            if path.is_symlink() or target.resolve() != target.absolute():
                raise OfficialError("REDIRECTED_STICKER")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    (state / "img_cache").mkdir(exist_ok=True)
    (runtime / "memory.md").write_text("旧记忆只通过本群明确映射导入，不加载全局记忆。\n", encoding="utf-8")
    fixed_json(runtime / "official_lane_config.json", cfg)
    if cfg.get("operation_mode") == "normal":
        from hakimi.chat.official_groups import adapt_group
        for route in store.get("group_routes", {}).values():
            adapt_group(runtime, route)
    from hakimi.ops import shims
    shims.write(runtime, shims.RUNTIME_SHIMS)
    fixed_json(state / "source_manifest.json", {"at": time.time(), "files": manifest})
    # Persist binding independently from code snapshots and never copy the old outbox/state.
    for old_qq, member_id in store.get("member_mapping", {}).items():
        map_member(old_qq, member_id, base)
    return runtime


def read_secret(base=BASE):
    path = base / "state/official_appsecret.dpapi"
    if not path.exists():
        raise OfficialError("CREDENTIALS_REQUIRED")
    # DPAPI CurrentUser: raw credentials are absent from copied model runtime/config.
    literal = str(path).replace("'", "''")
    script = ("$ErrorActionPreference='Stop'; $e=(Get-Content -LiteralPath '" + literal + "' -Raw).Trim(); $s=ConvertTo-SecureString $e; "
              "$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); "
              "try {[Console]::Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($p))} "
              "finally {[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    shell = shutil.which("pwsh.exe") or str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    environment = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, timeout=10, env=environment,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or not 8 <= len(result.stdout) <= 4096:
        raise OfficialError("CREDENTIAL_DECRYPT_FAILED")
    return result.stdout.decode("utf-8").strip()


def reviewed_memory(base=BASE):
    review = json.loads((base / "official_memory_review.json").read_text(encoding="utf-8"))
    group = settings(base)["test_group_number"]
    if review.get("source_group") == group:
        return review
    return review.get("group_reviews", {}).get(group, {})


def migrate_memory(base=BASE):
    """Only reviewed, explicitly group-scoped notes; no global impression dump."""
    cfg = settings(base)
    runtime = runtime_path(base)
    store = RouteStore(runtime / "state/official_routes.sqlite3")
    if not store.get("group_openid"):
        raise OfficialError("PAIR_TEST_GROUP_FIRST")
    group = "group:" + cfg["test_group_number"]
    target = runtime / "state/session_memory.jsonl"
    present = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()] if target.exists() else []
    seen = {r.get("migration_key") for r in present if r.get("migration_key")}
    source_path = base / "state/session_memory.jsonl"
    candidates = []
    if source_path.exists():
        for line in source_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("session_key") == group and row.get("kind", "preference") in {"preference", "lore"}:
                # Preserve original note dates/expiry. Bind names only after separate identity confirmation.
                candidates.append((row, "scoped:" + hashlib.sha256(line.encode()).hexdigest()))
    reviewed = reviewed_memory(base)
    # The live legacy file is still being updated by the old bot. Review a pinned
    # source snapshot, never silently expand an approval to newly appended text.
    snapshot = base / reviewed.get("source_snapshot", "state/memory_migration_sources/missing")
    valid_review = (reviewed.get("source_group") == cfg["test_group_number"]
                    and snapshot.is_file() and snapshot.resolve() == snapshot.absolute()
                    and snapshot.resolve().is_relative_to((base / "state/memory_migration_sources").resolve())
                    and hashlib.sha256(snapshot.read_bytes()).hexdigest() == reviewed.get("source_block_sha256"))
    if valid_review:
        for index, note in enumerate(reviewed.get("group_notes", [])):
            if note.get("session_key") == group:
                row = {"session_key": group, "kind": "preference", "created_at": time.time(),
                       "note": note["note"], "source": "reviewed_legacy_group_memory"}
                candidates.append((row, "legacy:" + reviewed["source_block_sha256"] + ":" + str(index)))
    added = 0
    for row, key in candidates:
        if key not in seen:
            present.append({**row, "migration_key": key, "migration_at": time.time()})
            seen.add(key)
            added += 1
    if added:
        temporary = target.with_suffix(".tmp")
        temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in present), encoding="utf-8")
        temporary.replace(target)
    store.set("memory_mapping", {"group": cfg["test_group_number"], "added": added, "at": time.time(),
                                 "reviewed_snapshot_valid": valid_review,
                                 "personal_identity_mapping": "not_inferred_from_nicknames"})
    return added


def map_member(old_qq, member_id, base=BASE):
    if not re.fullmatch(r"[1-9][0-9]{4,19}", old_qq) or not re.fullmatch(r"[1-9][0-9]{0,19}", member_id):
        raise OfficialError("INVALID_MEMBER_MAPPING")
    runtime = runtime_path(base)
    store = RouteStore(runtime / "state/official_routes.sqlite3")
    if not store.get("group_openid"):
        raise OfficialError("PAIR_TEST_GROUP_FIRST")
    with store.db() as db:
        if not db.execute("SELECT 1 FROM actors WHERE local_id=?", (member_id,)).fetchone():
            raise OfficialError("MEMBER_NOT_OBSERVED_IN_TEST_GROUP")
    mapping = store.get("member_mapping", {})
    if old_qq in mapping and mapping[old_qq] != member_id or member_id in mapping.values() and mapping.get(old_qq) != member_id:
        raise OfficialError("MEMBER_MAPPING_CONFLICT")
    mapping[old_qq] = member_id
    # Explicit operator confirmation, never a nickname-based inference.
    store.set("member_mapping", mapping)
    original = json.loads((base / "config.json").read_text(encoding="utf-8")).get("user_bias", {}).get("users", {}).get(old_qq, {})
    config = json.loads((runtime / "config.json").read_text(encoding="utf-8"))
    profile = {"nickname": str(original.get("nickname", "已确认群友"))[:80],
               "affinity": max(0, min(100, float(original.get("affinity", 50)))), "attitude": "已由本机用户确认身份的群友"}
    config["user_bias"]["users"][member_id] = profile
    fixed_json(runtime / "config.json", config)
    review = reviewed_memory(base)
    member = next((row for row in review.get("pending_members", []) if row.get("old_qq") == old_qq), {})
    target = runtime / "state/session_memory.jsonl"
    notes = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()] if target.exists() else []
    seen = {row.get("migration_key") for row in notes}
    for index, note in enumerate(member.get("notes", [])):
        key = "member:" + old_qq + ":" + member_id + ":" + str(index)
        if key not in seen:
            notes.append({"session_key": "group:" + settings(base)["test_group_number"], "kind": "preference",
                "created_at": time.time(), "migration_key": key,
                "note": "已由本机用户确认的成员（本地ID " + member_id + "）：" + note})
    if notes:
        temporary = target.with_suffix(".tmp")
        temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in notes), encoding="utf-8")
        temporary.replace(target)
    return {"old_qq": old_qq, "local_member_id": member_id, "confirmed_locally": True}


async def serve(base=BASE):
    runtime = runtime_path(base)
    cfg = settings(base)
    marker = base / "state/official_supervisor.json"
    delay = 2
    while True:
        cfg = settings(base)
        if cfg.get("enabled") is not True or not (base / "state/official_appsecret.dpapi").exists():
            fixed_json(marker, {"state": "awaiting_local_credentials", "test_group": cfg["test_group_number"],
                                "at": time.time(), "pid": os.getpid(), "qq_test_sent": False})
            await asyncio.sleep(5)
            continue
        # Start a separate Python interpreter: imported globals/default paths stay isolated.
        try:
            secret = read_secret(base)
            fixed_json(runtime / "official_lane_config.json", cfg)
            child_env = dict(os.environ)
            child_env["HAKIMI_OFFICIAL_APPSECRET"] = secret
            child_env["PYTHONUTF8"] = "1"
            fixed_json(marker, {"state": "running", "test_group": cfg["test_group_number"],
                                "at": time.time(), "pid": os.getpid()})
            with (runtime / "state/official_service.log").open("ab") as log:
                started = time.monotonic()
                proc = await asyncio.create_subprocess_exec(sys.executable, "-u", str(runtime / "official_lane.py"),
                    cwd=runtime, env=child_env, stdout=log, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                del child_env["HAKIMI_OFFICIAL_APPSECRET"]
                del secret
                waiter = asyncio.create_task(proc.wait())
                routes = RouteStore(runtime / "state/official_routes.sqlite3")
                memory_mapped = bool(routes.get("memory_mapping"))
                while proc.returncode is None:
                    try:
                        await asyncio.wait_for(asyncio.shield(waiter), timeout=3)
                    except asyncio.TimeoutError:
                        if not memory_mapped and routes.get("group_openid"):
                            migrate_memory(base)
                            memory_mapped = True
            routes.close()
            fatal = proc.returncode == 78
            if time.monotonic()-started >= 300:
                delay = 2
            fixed_json(marker, {"state": "stopped_check_configuration" if fatal else "restarting",
                                "exit_code": proc.returncode, "retry_seconds": 0 if fatal else delay,
                                "at": time.time(), "pid": os.getpid()})
            from hakimi.features.codex_help import report_fault
            report_fault(base, "official_worker", code="process_exit_" + str(proc.returncode))
            if fatal:
                return
            await asyncio.sleep(delay)
            delay = min(60, delay * 2)
        except OfficialError as exc:
            from hakimi.features.codex_help import report_fault
            report_fault(base, "official_supervisor", exc, code=exc.code)
            fixed_json(marker, {"state": "configuration_error", "code": exc.code, "at": time.time()})
            return


def worker_health(base=BASE, now=None):
    path = runtime_path(base) / "state/official_worker.json"
    if not path.exists():
        return {"online": False, "health": "not_started"}
    value = json.loads(path.read_text(encoding="utf-8"))
    age = (time.time() if now is None else now) - value.get("at", 0)
    fresh = 0 <= age < 15
    return {**value, "online": bool(value.get("online")) and fresh,
            "health": "fresh" if fresh else "stale", "heartbeat_age_seconds": round(age, 1)}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="哈基米 QQ 官方测试通道")
    parser.add_argument("command", choices=("prepare", "serve", "pair", "status", "migrate-memory", "check", "map-user", "resume-setup"))
    parser.add_argument("--old-qq", default="")
    parser.add_argument("--member-id", default="")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            print(json.dumps({"prepared": str(prepare()), "existing_bot_unchanged": True}, ensure_ascii=False))
        elif args.command == "resume-setup":
            if not (BASE / "state/official_appsecret.dpapi").is_file():
                raise OfficialError("CREDENTIALS_REQUIRED")
            runtime = prepare()
            store = RouteStore(runtime / "state/official_routes.sqlite3")
            if not store.get("group_openid"):
                code = store.arm()
                print("请由群主或管理员在 " + settings()["test_group_number"] + " 群 @官方机器人 发送：/绑定测试群 " + code)
            cfg = settings()
            cfg["enabled"] = True
            fixed_json(BASE / "official_test_config.json", cfg)
            print("已恢复配置，使用本机已加密保存的密钥；未发送QQ消息。")
        elif args.command == "serve":
            # Lifetime process lock, distinct from the production bridge locks.
            import msvcrt
            with (BASE / "state/official_supervisor.lock").open("a+b") as lock:
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise OfficialError("SUPERVISOR_ALREADY_RUNNING") from None
                asyncio.run(serve())
        elif args.command == "pair":
            store = RouteStore(runtime_path() / "state/official_routes.sqlite3")
            if store.get("group_openid"):
                print("测试群已绑定；不会通过群消息换绑其他群。")
            else:
                code = store.arm()
                print("请由群主或管理员在 " + settings()["test_group_number"] + " 群 @官方机器人 发送：/绑定测试群 " + code)
                print("15 分钟有效；绑定操作不发送机器人回复。")
        elif args.command == "migrate-memory":
            print(json.dumps({"imported_notes": migrate_memory(), "other_groups_untouched": True}))
        elif args.command == "map-user":
            print(json.dumps(map_member(args.old_qq, args.member_id), ensure_ascii=False))
        elif args.command == "check":
            cfg = settings()
            print(json.dumps({"app_id": cfg["app_id"], "test_group": cfg["test_group_number"],
                "enabled": cfg.get("enabled") is True, "credential_file_present": (BASE / "state/official_appsecret.dpapi").exists(),
                "runtime_prepared": (runtime_path() / "config.json").exists(), "network_called": False}))
        else:
            path = BASE / "state/official_supervisor.json"
            if path.exists():
                supervisor = json.loads(path.read_text(encoding="utf-8"))
                if worker_health().get("health") == "stale":
                    supervisor["state"] = "offline_stale_heartbeat"
                print(path.name + ": " + json.dumps(supervisor, ensure_ascii=False))
            print("official_worker.json: " + json.dumps(worker_health(), ensure_ascii=False))
            store = RouteStore(runtime_path() / "state/official_routes.sqlite3")
            print(json.dumps({"test_group_bound": bool(store.get("group_openid")),
                "groups": [{k: r.get(k) for k in ("local_id", "name", "legacy_group", "active")}
                           for r in store.get("group_routes", {}).values()],
                "last_event": store.get("last_event"), "last_send_error": store.get("last_send_error"),
                "memory_mapping": store.get("memory_mapping")}, ensure_ascii=False))
    except (OfficialError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "code": exc.code if isinstance(exc, OfficialError) else type(exc).__name__}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
