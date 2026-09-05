"""Exercise startup helpers without sourcing operator env or touching live PIDs."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/run_webhook_server.sh"


@pytest.mark.parametrize("remaining", ["", "222"])
def test_timeout_recheck_handles_empty_listener_array(remaining: str) -> None:
    source = SCRIPT.read_text()
    helpers = source.split("find_existing_server_pids() {", 1)[1]
    helpers = "find_existing_server_pids() {" + helpers.split("\nstop_existing_server\n", 1)[0]
    # find_existing_server_pids runs in a process substitution: distinguish the
    # second invocation using the state set by the mocked TERM signal instead.
    overrides = r'''
HOST=127.0.0.1
PORT=8022
phase=before
find_existing_server_pids() {
    if [[ "$phase" == before ]]; then printf '%s\n' 111; fi
}
is_codex_review_server() { return 0; }
related_server_pids() { for pid in "$@"; do printf '%s\n' "$pid"; done; }
snapshot_processes() { printf '%s\n' snapshot; }
wait_for_stop() { return 1; }
matching_snapshot_pids() { if [[ -n "$remaining" ]]; then printf '%s\n' "$remaining"; fi; }
kill() {
    if [[ "$1" == -TERM ]]; then phase=after; return 0; fi
    [[ "$*" == "-KILL 222" ]] || exit 98
    printf '%s\n' escalated
}
stop_existing_server
printf '%s\n' completed
'''
    result = subprocess.run(
        ["/bin/bash", "-s"], input=f"set -euo pipefail\nremaining='{remaining}'\n"
        + helpers + overrides, text=True, capture_output=True, timeout=3, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "completed" in result.stdout
    assert ("escalated" in result.stdout) is bool(remaining)
