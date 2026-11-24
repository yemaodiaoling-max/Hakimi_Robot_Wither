#!/usr/bin/env bash
# Install Hakimi on a Linux server (Ubuntu/Debian tested) as systemd *user* services.
#
#   deploy/install.sh --modules qq,video          # any combination of: qq, video, pet
#   deploy/install.sh --modules qq --vector       # + local semantic memory (fastembed, ~500 MB)
#   deploy/install.sh --no-systemd                # only create the venv and instance folder
#
# Paths (override with environment variables):
#   HAKIMI_APP      this checkout                      (default: the folder containing deploy/)
#   HAKIMI_ROOT     instance data folder               (default: ~/hakimi-data)
#   HAKIMI_VENV     Python virtual environment         (default: $HAKIMI_APP/.venv)
#   HAKIMI_GATEWAY_DIR  local model gateway files      (default: ~/.local/share/hakimi-gateway)
#
# Nothing here asks for keys: open the setup panel afterwards (the script prints how).
set -euo pipefail

APP=${HAKIMI_APP:-$(cd "$(dirname "$0")/.." && pwd)}
ROOT=${HAKIMI_ROOT:-$HOME/hakimi-data}
VENV=${HAKIMI_VENV:-$APP/.venv}
GATEWAY=${HAKIMI_GATEWAY_DIR:-$HOME/.local/share/hakimi-gateway}
VIDEO=${HAKIMI_VIDEO_DIR:-$APP/bili_companion}
UNITS=${HAKIMI_UNITS:-$HOME/.config/systemd/user}
MODULES=""
VECTOR=0
SYSTEMD=1

while [ $# -gt 0 ]; do
  case "$1" in
    --modules) MODULES="$2"; shift 2 ;;
    --modules=*) MODULES="${1#*=}"; shift ;;
    --vector) VECTOR=1; shift ;;
    --no-systemd) SYSTEMD=0; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
MODULES=$(echo "$MODULES" | tr -d ' ')
has() { case ",$MODULES," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
for m in ${MODULES//,/ }; do
  case "$m" in qq|video|pet) ;; *) echo "unknown module: $m (use qq, video, pet)" >&2; exit 2 ;; esac
done

PY=${PYTHON:-python3}
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || {
  echo "Python 3.12+ is required (found: $("$PY" --version 2>&1))" >&2; exit 1; }

echo "==> virtual environment: $VENV"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install -q --upgrade pip

EXTRAS=""
has qq && EXTRAS="qq"
has video && EXTRAS="${EXTRAS:+$EXTRAS,}video"
[ "$VECTOR" = 1 ] && EXTRAS="${EXTRAS:+$EXTRAS,}vector"
echo "==> installing hakimi${EXTRAS:+[$EXTRAS]}"
"$VENV/bin/pip" install -q -e "$APP${EXTRAS:+[$EXTRAS]}"
if has video; then
  echo "==> installing the video module (bili_companion)"
  "$VENV/bin/pip" install -q -e "$VIDEO[auth]"
  command -v ffmpeg >/dev/null || echo "   ! ffmpeg not found: sudo apt install ffmpeg (needed to watch videos)"
fi

echo "==> instance folder: $ROOT"
export HAKIMI_ROOT="$ROOT" HAKIMI_GATEWAY_DIR="$GATEWAY" HAKIMI_VIDEO_DIR="$VIDEO"
"$VENV/bin/hakimi" setup init --modules "core${MODULES:+,$MODULES}" >/dev/null
has qq && "$VENV/bin/python" -m hakimi.ops.shims --root "$ROOT" >/dev/null || true

if [ "$SYSTEMD" = 1 ] && command -v systemctl >/dev/null; then
  echo "==> systemd user units: $UNITS"
  mkdir -p "$UNITS"
  for unit in "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer; do
    [ -e "$unit" ] || continue
    sed -e "s#@ROOT@#$ROOT#g" -e "s#@RUNTIME@#$ROOT/official_test_runtime#g" -e "s#@VENV@#$VENV#g" \
        -e "s#@ENCODER_VENV@#$VENV#g" -e "s#@GATEWAY@#$GATEWAY#g" -e "s#@VIDEO@#$VIDEO#g" \
        "$unit" > "$UNITS/$(basename "$unit").new"
    mv "$UNITS/$(basename "$unit").new" "$UNITS/$(basename "$unit")"
  done
  systemctl --user daemon-reload
  # Core services start now; module services start after you save the setup panel.
  systemctl --user enable --now hakimi-gateway.service hakimi-memory-console.service
  for unit in $("$VENV/bin/hakimi" modules units); do
    case "$unit" in hakimi-gateway.service|hakimi-memory-console.service) ;; *) systemctl --user enable "$unit" >/dev/null 2>&1 || true ;; esac
  done
  if command -v loginctl >/dev/null && [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    echo "   ! to keep the bot running after you log out: sudo loginctl enable-linger $USER"
  fi
fi

cat <<EOF

Done. Next steps
  1. Open the setup panel. On your own computer run:
       ssh -L 18768:127.0.0.1:18768 $USER@<this-server>
     then on the server:
       HAKIMI_ROOT=$ROOT $VENV/bin/hakimi setup url
     and open the printed http://127.0.0.1:18768/setup.html#token=... address in your browser.
  2. Fill in the model API key, QQ bot / Bilibili accounts, and save with "重启相关服务" ticked.
  3. Check:  systemctl --user status 'hakimi-*'
EOF
