"""Gemini waiter. Claims a persisted generation turn; dsh may take over on failure."""
from hakimi import paths

import json
import sys
import time
from pathlib import Path
from hakimi.prompt.prompt_context import activate_context, render_prompt
from hakimi.models.fallback_supervisor import coordinator_for
from hakimi.core.runtime_io import read_json_cached

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = paths.root()
PENDING_TRIGGER = BASE_DIR / "state" / "pending_trigger.json"
WAITING_FLAG = BASE_DIR / "state" / "waiting.flag"
MEMORY_PATH = BASE_DIR / "memory.md"
STYLE_PATH = paths.persona("style_guide.md", BASE_DIR)
COLLECTED_DIR = BASE_DIR / "stickers" / "collected"


def get_collected_stickers_str() -> str:
    if not COLLECTED_DIR.exists():
        return "暂无"
    files = sorted(f.name for f in COLLECTED_DIR.iterdir() if f.is_file())
    return ", ".join(files) if files else "暂无"


def main():
    print("正在等待下一个通过【偏向与随机检定】的 QQ 消息事件...")
    cfg = read_json_cached(BASE_DIR / "config.json")
    coordinator = coordinator_for(BASE_DIR, cfg)
    # Do not complete someone else's turn by reading a mutable shared lease.
    # The owner must explicitly reply or skip with its original token.
    WAITING_FLAG.write_text(str(time.time()), encoding="utf-8")
    last_heartbeat = time.monotonic()
    try:
        while True:
            try:
                claim = coordinator.claim_primary()
                if claim:
                    data = claim["payload"]
                    try:
                        prompt = render_prompt(data, claim=claim)
                        activate_context(data, claim=claim)
                    except Exception:
                        coordinator.report_failure(claim["token"], "prompt_render_error")
                        raise
                    WAITING_FLAG.unlink(missing_ok=True)
                    PENDING_TRIGGER.unlink(missing_ok=True)
                    print(prompt)
                    return 0
                # Optional isolated official test lane. Production always gets
                # first claim opportunity; no test paths/tokens enter its context.
                from hakimi.transport.official_waiter import try_official_claim
                official_prompt = try_official_claim(BASE_DIR)
                if official_prompt is not None:
                    WAITING_FLAG.unlink(missing_ok=True)
                    print(official_prompt)
                    return 0
                if time.monotonic() - last_heartbeat >= 5:
                    WAITING_FLAG.write_text(str(time.time()), encoding="utf-8")
                    last_heartbeat = time.monotonic()
            except Exception as e:
                print(f"读取信标出错，重试中: {type(e).__name__}")
            time.sleep(0.5)
    finally:
        WAITING_FLAG.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException:
        try:
            with open(BASE_DIR / "state" / "waiter_crash.log", "a", encoding="utf-8") as f:
                import traceback
                f.write("\n--- Crash ---\n")
                traceback.print_exc(file=f)
        except Exception:
            pass
        raise

