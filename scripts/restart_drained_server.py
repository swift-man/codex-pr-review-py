"""Restart only the authenticated, drained Codex listener on fixed port 8022."""

from __future__ import annotations

import fcntl
import http.client
import json
import os
import re
import stat
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8022


def command(*argv: str) -> str:
    return subprocess.check_output(argv, timeout=3, text=True).strip()


def status(secret: str) -> dict:
    return control_request(secret, "status")


def control_request(secret: str, action: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/internal/control/{action}",
        headers={"X-Gorani-Bot-Control-Secret": secret, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        raw = response.read(16385)
    if len(raw) > 16384:
        raise ValueError("control response too large")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("invalid control response")
    return result


def listeners() -> set[int]:
    result = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-a", f"-iTCP:{PORT}", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if result.returncode not in (0, 1) or result.stderr:
        raise ValueError("listener inspection failed")
    return {int(value) for value in result.stdout.split()}


def identity(pid: int) -> str:
    return command("/bin/ps", "-ww", "-p", str(pid), "-o", "uid=,lstart=,command=")


def owned_descriptor(path: Path, *, append: bool = False) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags | (os.O_APPEND if append else 0), 0o600)
    metadata = os.fstat(fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        os.close(fd)
        raise ValueError("unsafe runtime file")
    return fd


def restart(log_fd: int, secret: str) -> None:
    before = status(secret)
    pid = before.get("pid")
    if (
        type(pid) is not int
        or pid <= 1
        or before.get("draining") is not True
        or not isinstance(before.get("instanceId"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", before["instanceId"])
        or not isinstance(before.get("operationId"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", before["operationId"])
        or type(before.get("queueDepth")) is not int
        or before["queueDepth"] != 0
        or type(before.get("activeJobs")) is not int
        or before["activeJobs"] != 0
        or listeners() != {pid}
    ):
        raise ValueError("listener is not exclusively owned and drained")
    captured = identity(pid)
    if (
        captured.split()[0] != str(os.geteuid())
        or "codex_review.main:app_factory" not in captured
        or "uvicorn" not in captured
        or identity(pid) != captured
    ):
        raise ValueError("unexpected listener identity")
    try:
        handoff = control_request(secret, "commit-restart", {
            "instanceId": before["instanceId"], "operationId": before["operationId"],
        })
    except urllib.error.HTTPError:
        raise
    except (OSError, http.client.HTTPException):
        # The server owns shutdown even if its acknowledgement is lost.
        # Never signal the old PID; only launch after the port becomes free.
        pass
    else:
        if handoff != {
            "status": "restart-committed", "instanceId": before["instanceId"],
            "operationId": before["operationId"], "pid": pid,
        }:
            raise ValueError("restart handoff changed")
    deadline = time.monotonic() + 15
    while listeners():
        if time.monotonic() >= deadline:
            raise TimeoutError("listener did not stop")
        time.sleep(0.2)
    process = subprocess.Popen(
        [
            str(ROOT / ".venv/bin/python"),
            "-m",
            "uvicorn",
            "codex_review.main:app_factory",
            "--factory",
            "--no-proxy-headers",
            "--host",
            "0.0.0.0",
            "--port",
            str(PORT),
            "--log-level",
            "info",
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=log_fd,
        # Keep the daemon in the admin command's owned process group until the
        # wrapper exits successfully. Cancellation/timeout can then reap it too;
        # the admin runner preserves this group only on a successful exit.
        start_new_session=False,
        env={**os.environ, "PORT": str(PORT)},
    )
    verified = False
    try:
        deadline = time.monotonic() + 45
        while process.poll() is None and time.monotonic() < deadline:
            try:
                after = status(secret)
                if (
                    isinstance(after.get("instanceId"), str)
                    and re.fullmatch(r"[0-9a-f]{32}", after["instanceId"])
                    and after.get("instanceId") != before.get("instanceId")
                    and after.get("pid") == process.pid
                    and listeners() == {process.pid}
                    and after.get("draining") is False
                ):
                    verified = True
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.25)
        raise TimeoutError("new listener did not become ready")
    finally:
        if not verified:
            process.kill()
            process.wait(timeout=5)


def main() -> int:
    secret = os.environ.get("CODEX_CONTROL_SHARED_SECRET", "")
    if not re.fullmatch(r"[0-9a-f]{64}", secret):
        return 78
    runtime = ROOT / ".admin-runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    metadata = runtime.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or runtime.resolve() != runtime
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        return 78
    with os.fdopen(owned_descriptor(runtime / "restart.lock"), "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with os.fdopen(owned_descriptor(runtime / "codex-review.log", append=True), "a") as log:
            restart(log.fileno(), secret)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError):
        # No raw config, command output or auth material in control-plane output.
        raise SystemExit("Codex restart failed; inspect the private bot log") from None
