"""Optional official lane for the existing Gemini waiter; no background renewal."""
import json
from pathlib import Path
import sys
import time

from hakimi.models.fallback_supervisor import coordinator_for
from hakimi.prompt.prompt_context import activate_context, render_prompt
from hakimi.core.runtime_io import read_json_cached


def render_official_prompt(payload,runtime,claim,normal_mode=True):
    command='& "'+sys.executable+'" "'+str(runtime/"qq_cli.py")+'"'
    prompt=render_prompt(payload,base=runtime,claim=claim,cli_label="QQCLI")
    heading="QQ 官方通道（日常运行）" if normal_mode else "QQ 官方测试通道"
    return ("=== "+heading+"：本轮不是旧版 QQ 账号 ===\n"
            "本轮命令前缀（所有示例中的QQCLI须替换为此完整前缀再执行）：\n"+command+"\n"
            "仅使用此绝对路径，不能执行字面QQCLI或旧版目录下的qq_cli.py。"
            "保持哈基米原有人设，不向群友讲通道、测试、模型切换或内部细节。"
            "本轮结束回到原来的wait_next_event.py继续等待。\n"+prompt)


def try_official_claim(base, now=None):
    now = time.time() if now is None else now
    runtime = Path(base) / "official_test_runtime"
    marker = runtime / "state/official_worker.json"
    if runtime.is_symlink() or runtime.resolve() != runtime.absolute() or not marker.exists():
        return None
    try:
        health = read_json_cached(marker, readonly=True)
        if not health.get("online") or not health.get("bound") or not 0 <= now - health.get("at", 0) < 15:
            return None
        cfg = read_json_cached(runtime / "config.json", readonly=True)
        if cfg.get("official_lane") is not True:
            return None
        coordinator = coordinator_for(runtime, cfg)
        claim = coordinator.claim_primary()
        if not claim:
            return None
        try:
            prompt = render_official_prompt(claim["payload"],runtime,claim,cfg.get("official_operation_mode")=="normal")
            activate_context(claim["payload"], base=runtime, claim=claim)
            # All tool paths are explicit. Never let a test turn use production CLI/state.
        except Exception:
            coordinator.report_failure(claim["token"], "prompt_render_error")
            raise
        (runtime / "state/pending_trigger.json").unlink(missing_ok=True)
        return prompt
    except (OSError, ValueError):
        return None
