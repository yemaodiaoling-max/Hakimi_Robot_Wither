#!/bin/bash
# Execute only after reviewing the uploaded source release. No QQ restart here.
set -euo pipefail
ROOT=/opt/hakimi/bili_companion
PY=/opt/hakimi/venv/bin/python
test "$(pwd -P)" = "$ROOT"
test -f "$ROOT/src/bili_companion/integrations/service.py"
test -f /opt/hakimi/data/official_test_runtime/state/memory.sqlite3
export PYTHONPATH="$ROOT/src"
export PYTHONDONTWRITEBYTECODE=1
umask 077
mkdir -p "$ROOT/state/bridge" "$ROOT/state/deployment_backups" "$HOME/.config/systemd/user"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$ROOT/state/deployment_backups/$STAMP"
mkdir "$BACKUP"
for NAME in hakimi-memory-bridge.service hakimi-memory-bridge.timer; do
    if test -f "$HOME/.config/systemd/user/$NAME"; then
        cp -p "$HOME/.config/systemd/user/$NAME" "$BACKUP/$NAME"
    fi
done
if test -f "$ROOT/deploy/bridge.json"; then
    cp -p "$ROOT/deploy/bridge.json" "$BACKUP/bridge.json"
else
    cp "$ROOT/deploy/bridge.example.json" "$ROOT/deploy/bridge.json"
fi
# New dedicated, empty native store only. No import from examples or old state.
"$PY" - <<'PY'
from pathlib import Path
import json
from bili_companion.memory import MemoryStore
from bili_companion.runtime.storage import Database
path=Path('/opt/hakimi/bili_companion/state/native_watch.sqlite3')
config=json.loads(Path('/opt/hakimi/bili_companion/deploy/bridge.json').read_text())
expected={
    'qq_database':'/opt/hakimi/data/official_test_runtime/state/memory.sqlite3',
    'qq_snapshot':'/opt/hakimi/data/official_test_runtime/state/bili_bridge_context.json',
    'state_dir':'/opt/hakimi/bili_companion/state/bridge',
    'bilibili_database':str(path),'bilibili_account_id':'hakimi-bilibili',
    'qq_identity':'hakimi-qq','bilibili_identity':'hakimi-bilibili'}
if any(config.get(key)!=value for key,value in expected.items()):
    raise SystemExit('Bridge config differs from reviewed production paths/identities')
if not path.exists():
    MemoryStore(Database(path))
PY
"$PY" -m bili_companion.integrations.service --config "$ROOT/deploy/bridge.json"
install -m 600 "$ROOT/deploy/hakimi-memory-bridge.service" "$HOME/.config/systemd/user/hakimi-memory-bridge.service"
install -m 600 "$ROOT/deploy/hakimi-memory-bridge.timer" "$HOME/.config/systemd/user/hakimi-memory-bridge.timer"
systemctl --user daemon-reload
systemctl --user enable --now hakimi-memory-bridge.timer
systemctl --user start hakimi-memory-bridge.service
systemctl --user is-active hakimi-memory-bridge.timer
printf 'Bridge installed. QQ source/hook/services untouched. Unit backups: %s\n' "$BACKUP"
