"""Release the checked-out code to the running services (replaces cloud_apply_code.sh).

    python -m hakimi.ops.release [--skip-tests] [--services agent,worker,...]

1. run the test suite of the checkout (tests/run_tests.py);
2. check the instance configuration files (hakimi.config);
3. wait until no reply is being generated or delivered;
4. restart the services and confirm the official worker is back online.
Nothing is copied: services run the installed (editable) package directly.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from hakimi import paths

SERVICES = ("hakimi-worker", "hakimi-agent", "hakimi-help", "hakimi-memory-console", "hakimi-memory-embedding")


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def lane_busy(runtime, now=None):
    now = time.time() if now is None else now
    from hakimi.bridge.turn_lanes import LANES
    turns = [read(runtime/"state"/("turn_lanes/"+lane+".json" if lane else "turn_state.json")) for lane in LANES]
    delivery = read(runtime / "state/delivery_state.json")
    active = any(turn.get("status") in {"queued", "primary_active", "fallback_active"} and turn.get("expires_at", now + 1) > now for turn in turns)
    return active or bool(delivery.get("pending"))


def run_tests(checkout):
    result = subprocess.run([sys.executable, "-B", str(checkout / "tests/run_tests.py")], cwd=checkout,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail = "\n".join(result.stdout.strip().splitlines()[-3:])
    print(tail, flush=True)
    return result.returncode == 0


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def worker_online(runtime, since):
    health = read(runtime / "state/official_worker.json")
    return bool(health.get("online")) and health.get("at", 0) >= since


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--services", default=",".join(SERVICES))
    parser.add_argument("--wait", type=int, default=180, help="seconds to wait for an idle lane")
    args = parser.parse_args(argv)
    checkout, runtime = paths.checkout_root(), paths.runtime()
    if not args.skip_tests:
        if checkout is None:
            raise SystemExit("tests need a source checkout; use --skip-tests for a wheel install")
        if not run_tests(checkout):
            raise SystemExit("tests failed; running services unchanged")
    from hakimi import config
    errors = [f for f in config.check() if f[0] == "error"]
    for _, label, message in errors:
        print(f"config error: {label}: {message}", flush=True)
    if errors:
        raise SystemExit("instance configuration has errors; running services unchanged")
    deadline = time.time() + args.wait
    while lane_busy(runtime):
        if time.time() > deadline:
            raise SystemExit("lane still busy; running services unchanged")
        time.sleep(1)
    services = [s.strip() for s in args.services.split(",") if s.strip()]
    started = time.time()
    result = systemctl("restart", *services)
    if result.returncode:
        raise SystemExit("restart failed: " + result.stdout.strip())
    for _ in range(60):
        time.sleep(1)
        if "hakimi-worker" not in services or worker_online(runtime, started):
            break
    states = {s: systemctl("is-active", s).stdout.strip() for s in services}
    ok = all(v == "active" for v in states.values()) and ("hakimi-worker" not in services or worker_online(runtime, started))
    print(json.dumps({"ok": ok, "services": states, "worker_online": worker_online(runtime, started)}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
