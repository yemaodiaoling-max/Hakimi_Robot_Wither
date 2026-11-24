"""Low-frequency local worker; Codex starts only for queued work."""
from hakimi import paths
import argparse
import json
import os
from pathlib import Path
import time

from hakimi.features.codex_help import HelpQueue, REVISION, report_fault, project_root, settings
from hakimi.features.codex_help_runner import run_codex, CodexHelpError

BASE = paths.root()


def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def monitor_health(base, now, started_at):
    if now-started_at < 90:
        return
    try:
        maintenance=json.loads((project_root(base)/"state/service_maintenance.json").read_text(encoding="utf-8-sig"))
        if now<maintenance.get("until",0):return
    except (OSError,ValueError,TypeError):pass
    for lane, name, component in ((base,"bridge_worker.json","legacy_bridge"),
                                  (base/"official_test_runtime","official_worker.json","official_worker")):
        if component=='legacy_bridge' and os.environ.get('HAKIMI_OFFICIAL_ONLY')=='1':
            continue
        try:
            value = json.loads((lane/"state"/name).read_text(encoding="utf-8"))
            age = now-value.get("at", 0)
            if age > 90:
                report_fault(base, component, code="heartbeat_stale")
            elif component == "legacy_bridge" and value.get("offline_since") and now-value["offline_since"] > 90:
                report_fault(base, component, code="onebot_disconnected")
            elif component == "official_worker" and not value.get("online") and age < 15:
                # A transient reconnect is not a fault; the adapter reports lasting errors itself.
                if value.get("last_error"):
                    report_fault(base, component, code="official_disconnected")
        except FileNotFoundError:
            report_fault(base, component, code="heartbeat_missing")
        except (OSError, ValueError, TypeError):
            report_fault(base, component, code="heartbeat_invalid")


def process_one(queue, runner=run_codex):
    job = queue.claim()
    if not job:
        return False
    try:
        result, usage = runner(job["brief"], queue.root, queue.cfg["timeout_seconds"])
        queue.finish(job["id"], result=result, usage=usage)
        diagnostic_path = queue.root/"state/codex_help_startup_diagnostic.json"
        if diagnostic_path.exists():
            atomic_json(diagnostic_path, {"at":time.time(),"job_id":job["id"],"state":"resolved","error":None})
    except Exception as exc:
        error = str(exc) if isinstance(exc, CodexHelpError) else type(exc).__name__
        queue.finish(job["id"], error=error)
        diagnostic = getattr(exc, "local_diagnostic", None)
        if diagnostic:
            atomic_json(queue.root/"state/codex_help_startup_diagnostic.json",
                        {"at":time.time(),"job_id":job["id"],"state":"failed","error":error,"detail":diagnostic})
    row = queue.get(job["id"])
    # Fault reports are for the owner; no route from this service to QQ exists.
    if row["kind"] == "fault":
        report = {key: row.get(key) for key in ("id","kind","state","repeats","created","finished","error","result")}
        report["fault"] = job["brief"]
        atomic_json(queue.root/"state/codex_help_latest_fault.json", report)
    return True


def serve(base=BASE):
    root = project_root(base)
    marker = root/"state/codex_help_worker.json"
    with (root/"state/codex_help_worker.lock").open("a+b") as lock:
        lock.write(b"0"); lock.flush(); lock.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        started = time.time()
        while True:
            queue = HelpQueue(root)
            atomic_json(marker, {"at":time.time(),"pid":os.getpid(),"revision":REVISION,"enabled":queue.cfg["enabled"]})
            monitor_health(root, time.time(), started)
            # Keep the heartbeat fresh while a bounded consultation is running.
            import threading
            finished = threading.Event()
            failure = []
            def perform():
                try:
                    process_one(queue)
                except Exception as exc:
                    failure.append(type(exc).__name__)
                finally:
                    finished.set()
            # No thread/process is started for idle polling.
            with queue.db() as db:
                has_work = db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') LIMIT 1").fetchone()
            if has_work:
                worker = threading.Thread(target=perform, daemon=True)
                worker.start()
                while not finished.wait(5):
                    atomic_json(marker, {"at":time.time(),"pid":os.getpid(),"revision":REVISION,"enabled":queue.cfg["enabled"]})
                if failure:
                    atomic_json(root/"state/codex_help_service_error.json", {"at":time.time(),"type":failure[0]})
            time.sleep(3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve","status","once","report-exit","busy"])
    parser.add_argument("--component", default="service")
    parser.add_argument("--code", default="process_exit")
    args = parser.parse_args()
    if args.command == "serve":
        serve()
    elif args.command == "status":
        print(json.dumps(HelpQueue(BASE).summary(), ensure_ascii=False))
    elif args.command == "report-exit":
        print(json.dumps(report_fault(BASE, args.component, code=args.code)))
    elif args.command == "busy":
        with HelpQueue(BASE).db() as db:
            print(int(db.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone() is not None))
    else:
        process_one(HelpQueue(BASE))
