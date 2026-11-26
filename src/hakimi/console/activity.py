"""Read the optional video companion activity for the console."""
import json
import os
import subprocess
import sys

from hakimi import modules


def activity_payload() -> dict:
    """Video activity for the console; empty (not an error) when the module is off."""
    if not modules.is_enabled("video") or not modules.installed("video"):
        return {"available": False, "reason": "module_disabled"}
    video = modules.video_dir()
    env = dict(os.environ, PYTHONPATH=str(video / "src"))
    result = subprocess.run([sys.executable, "-m", "bili_companion.panel.export_activity"], cwd=str(video), env=env,
                            capture_output=True, timeout=12)
    if result.returncode:
        return {"available": False, "reason": "export_failed"}
    return dict(json.loads(result.stdout), available=True)
