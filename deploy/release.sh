#!/usr/bin/env bash
# Test the checkout, wait for an idle lane, restart services. See hakimi/ops/release.py.
set -euo pipefail
APP=${HAKIMI_APP:-$(cd "$(dirname "$0")/.." && pwd)}
VENV=${HAKIMI_VENV:-$APP/.venv}
export HAKIMI_ROOT=${HAKIMI_ROOT:-$HOME/hakimi-data}
exec "$VENV/bin/python" -m hakimi.ops.release "$@"
