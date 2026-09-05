#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 || "$1" != "--no-tail" ]]; then
  echo "usage: $0 --no-tail (requires authenticated drain first)" >&2
  exit 64
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# This operator-owned startup file is also used by run_webhook_server.sh.
# shellcheck disable=SC1091
source "$ROOT/scripts/local_review_env.sh"
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/restart_drained_server.py"
