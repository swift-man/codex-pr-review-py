#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

ENV_FILE="$HERE/local_review_env.sh"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy from local_review_env.example.sh" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

: "${GITHUB_APP_ID:?GITHUB_APP_ID is required}"
: "${GITHUB_WEBHOOK_SECRET:?GITHUB_WEBHOOK_SECRET is required}"

if [[ -z "${GITHUB_APP_PRIVATE_KEY_PATH:-}" && -z "${GITHUB_APP_PRIVATE_KEY:-}" ]]; then
  echo "Either GITHUB_APP_PRIVATE_KEY_PATH or GITHUB_APP_PRIVATE_KEY must be set" >&2
  exit 1
fi

VENV_DIR="${VENV_DIR:-$ROOT/.venv}"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

find_existing_server_pids() {
    if ! command -v lsof >/dev/null 2>&1; then
        return 0
    fi

    lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

is_codex_review_server() {
    local pid="$1"
    local command_line
    command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    [[ "$command_line" == *"codex_review.main:app_factory"* ]]
}

is_process_running() {
    local pid="$1"
    local stat
    stat="$(ps -p "$pid" -o stat= 2>/dev/null | tr -d '[:space:]' || true)"
    [[ -n "$stat" && "$stat" != Z* ]]
}

collect_descendant_pids() {
    local parent="$1"
    local child

    while IFS= read -r child; do
        [[ -n "$child" ]] || continue
        echo "$child"
        collect_descendant_pids "$child"
    done < <(pgrep -P "$parent" 2>/dev/null || true)
}

dedupe_pids() {
    awk 'NF && !seen[$0]++'
}

related_server_pids() {
    local pid
    for pid in "$@"; do
        echo "$pid"
        collect_descendant_pids "$pid"
    done | dedupe_pids
}

wait_for_stop() {
    local all_stopped
    local pid
    for _ in {1..20}; do
        all_stopped=1
        for pid in "$@"; do
            if is_process_running "$pid"; then
                all_stopped=0
                break
            fi
        done
        [[ "$all_stopped" -eq 1 ]] && return 0
        sleep 0.5
    done

    for pid in "$@"; do
        is_process_running "$pid" && return 1
    done
    return 0
}

stop_existing_server() {
    local pids=()
    local related=()
    local pid

    while IFS= read -r pid; do
        [[ -n "$pid" ]] && pids+=("$pid")
    done < <(find_existing_server_pids)

    [[ "${#pids[@]}" -eq 0 ]] && return 0

    for pid in "${pids[@]}"; do
        if ! is_codex_review_server "$pid"; then
            echo "Port $PORT is already used by a non codex-review process (pid: $pid)" >&2
            echo "Stop it manually or choose another PORT." >&2
            exit 1
        fi
    done

    while IFS= read -r pid; do
        [[ -n "$pid" ]] && related+=("$pid")
    done < <(related_server_pids "${pids[@]}")

    echo "Stopping existing codex-review server on $HOST:$PORT (pid: ${related[*]})"
    kill -TERM "${related[@]}" 2>/dev/null || true
    if wait_for_stop "${related[@]}"; then
        return 0
    fi

    related=()
    while IFS= read -r pid; do
        [[ -n "$pid" ]] && related+=("$pid")
    done < <(related_server_pids "${pids[@]}")

    echo "Existing server did not stop after 10s; sending SIGKILL (pid: ${related[*]})" >&2
    kill -KILL "${related[@]}" 2>/dev/null || true
    wait_for_stop "${related[@]}" || true
}

stop_existing_server

exec uvicorn codex_review.main:app_factory \
    --factory \
    --host "$HOST" \
    --port "$PORT" \
    --log-level info
