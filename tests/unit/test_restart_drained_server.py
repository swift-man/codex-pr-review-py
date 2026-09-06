import fcntl
import importlib.util
import json
import os
import signal
import stat
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def runner() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts/restart_drained_server.py"
    spec = importlib.util.spec_from_file_location("restart_drained_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def environment(runner: ModuleType, monkeypatch: pytest.MonkeyPatch) -> dict:
    before = {
        "pid": 4321, "draining": True, "queueDepth": 0, "activeJobs": 0,
        "instanceId": "a" * 32,
        "operationId": "b" * 64,
    }
    mocks = {
        "status": MagicMock(return_value=before),
        "listeners": MagicMock(return_value={4321}),
        "identity": MagicMock(return_value=f"{os.geteuid()} uvicorn codex_review.main:app_factory"),
        "kill": MagicMock(), "killpg": MagicMock(), "popen": MagicMock(),
        "control_request": MagicMock(return_value={
            "status": "restart-committed", "pid": 4321,
            "instanceId": "a" * 32, "operationId": "b" * 64,
        }),
    }
    for name in ("status", "listeners", "identity", "control_request"):
        monkeypatch.setattr(runner, name, mocks[name])
    monkeypatch.setattr(runner.os, "kill", mocks["kill"])
    monkeypatch.setattr(runner.os, "killpg", mocks["killpg"])
    monkeypatch.setattr(runner.subprocess, "Popen", mocks["popen"])
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    return {"before": before, **mocks}


@pytest.mark.parametrize("field,value", [
    ("pid", True), ("pid", 1), ("draining", False),
    ("queueDepth", 1), ("activeJobs", 1), ("queueDepth", False), ("activeJobs", False),
    ("operationId", None), ("operationId", "invalid"),
])
def test_restart_refuses_undrained_or_invalid_status(
    runner: ModuleType, environment: dict, field: str, value: object,
) -> None:
    environment["before"][field] = value
    with pytest.raises(ValueError, match="exclusively owned and drained"):
        runner.restart(10, "a" * 64)
    environment["kill"].assert_not_called()
    environment["popen"].assert_not_called()


@pytest.mark.parametrize("listeners", [{9999}, {4321, 9999}, set()])
def test_restart_never_signals_a_different_or_shared_listener(
    runner: ModuleType, environment: dict, listeners: set[int],
) -> None:
    environment["listeners"].return_value = listeners
    with pytest.raises(ValueError):
        runner.restart(10, "a" * 64)
    environment["kill"].assert_not_called()


@pytest.mark.parametrize("identity", [
    "999999 uvicorn codex_review.main:app_factory", "0 unrelated-server",
])
def test_restart_rejects_unowned_or_unexpected_process_identity(
    runner: ModuleType, environment: dict, identity: str,
) -> None:
    environment["identity"].return_value = identity
    with pytest.raises(ValueError, match="identity"):
        runner.restart(10, "a" * 64)
    environment["kill"].assert_not_called()


def test_restart_rechecks_identity_before_signalling(runner: ModuleType, environment: dict) -> None:
    environment["identity"].side_effect = [
        f"{os.geteuid()} original uvicorn codex_review.main:app_factory",
        f"{os.geteuid()} replaced uvicorn codex_review.main:app_factory",
    ]
    with pytest.raises(ValueError, match="identity"):
        runner.restart(10, "a" * 64)
    environment["kill"].assert_not_called()


def test_old_listener_shutdown_timeout_never_launches_replacement(
    runner: ModuleType, environment: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.time, "monotonic", MagicMock(side_effect=[0, 16]))
    with pytest.raises(TimeoutError, match="listener did not stop"):
        runner.restart(10, "a" * 64)
    environment["kill"].assert_called_once_with(4321, signal.SIGTERM)
    environment["popen"].assert_not_called()


def test_restart_verifies_new_instance_and_leaves_it_running(
    runner: ModuleType, environment: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PORT", "9999")
    process = environment["popen"].return_value
    process.pid = 5432
    process.poll.return_value = None
    environment["status"].side_effect = [environment["before"], {
        "pid": 5432, "draining": False, "instanceId": "b" * 32,
    }]
    environment["listeners"].side_effect = [{4321}, {4321}, set(), {5432}]
    runner.restart(10, "a" * 64)
    environment["kill"].assert_called_once_with(4321, signal.SIGTERM)
    environment["control_request"].assert_called_once_with("a" * 64, "commit-restart", {
        "instanceId": "a" * 32, "operationId": "b" * 64,
    })
    environment["popen"].assert_called_once()
    args, kwargs = environment["popen"].call_args
    assert args[0] == [
        str(runner.ROOT / ".venv/bin/python"), "-m", "uvicorn",
        "codex_review.main:app_factory", "--factory", "--no-proxy-headers",
        "--host", "0.0.0.0", "--port", "8022", "--log-level", "info",
    ]
    assert kwargs["start_new_session"] is False
    assert kwargs["env"]["PORT"] == "8022"
    environment["killpg"].assert_not_called()
    process.kill.assert_not_called()
    process.wait.assert_not_called()


@pytest.mark.parametrize("after", [
    {"pid": 5432, "draining": False, "instanceId": "a" * 32},
    {"pid": 9999, "draining": False, "instanceId": "b" * 32},
    {"pid": 5432, "draining": True, "instanceId": "b" * 32},
    {"pid": 5432, "draining": False, "instanceId": None},
])
def test_unverified_startup_kills_and_reaps_newborn_in_owned_parent_group(
    runner: ModuleType, environment: dict, after: dict,
) -> None:
    process = environment["popen"].return_value
    process.pid = 5432
    process.poll.side_effect = [None, 1]
    environment["status"].side_effect = [environment["before"], after]
    environment["listeners"].side_effect = [{4321}, {4321}, set(), {5432}]
    with pytest.raises(TimeoutError, match="new listener"):
        runner.restart(10, "a" * 64)
    environment["killpg"].assert_not_called()
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5)


def test_startup_rejects_shared_listener_and_cleans_up_newborn(
    runner: ModuleType, environment: dict,
) -> None:
    process = environment["popen"].return_value
    process.pid = 5432
    process.poll.side_effect = [None, 1]
    environment["status"].side_effect = [environment["before"], {
        "pid": 5432, "draining": False, "instanceId": "b" * 32,
    }]
    environment["listeners"].side_effect = [{4321}, {4321}, set(), {5432, 9999}]
    with pytest.raises(TimeoutError, match="new listener"):
        runner.restart(10, "a" * 64)
    environment["killpg"].assert_not_called()
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5)


def test_status_uses_fixed_loopback_without_environment_proxy(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = MagicMock()
    response = opener.open.return_value.__enter__.return_value
    response.read.return_value = json.dumps({"pid": 4321}).encode()
    proxy = MagicMock()
    build = MagicMock(return_value=opener)
    monkeypatch.setattr(runner.urllib.request, "ProxyHandler", proxy)
    monkeypatch.setattr(runner.urllib.request, "build_opener", build)
    assert runner.status("a" * 64) == {"pid": 4321}
    proxy.assert_called_once_with({})
    request = opener.open.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:8022/internal/control/status"
    assert dict(request.header_items())["X-gorani-bot-control-secret"] == "a" * 64
    response.read.assert_called_once_with(16385)


def test_restart_does_not_signal_when_resume_invalidates_handoff(
    runner: ModuleType, environment: dict,
) -> None:
    environment["control_request"].side_effect = ValueError("drain resumed; job accepted")
    with pytest.raises(ValueError, match="drain resumed"):
        runner.restart(10, "a" * 64)
    environment["kill"].assert_not_called()
    environment["popen"].assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("instanceId", "c" * 32), ("operationId", "d" * 64), ("pid", 9999),
])
def test_restart_does_not_signal_on_mismatched_handoff(
    runner: ModuleType, environment: dict, field: str, value: object,
) -> None:
    environment["control_request"].return_value[field] = value
    with pytest.raises(ValueError, match="handoff"):
        runner.restart(10, "a" * 64)
    environment["kill"].assert_not_called()


def test_log_descriptors_append_without_overwriting_old_server_shutdown(
    runner: ModuleType, tmp_path: Path,
) -> None:
    path = tmp_path / "server.log"
    old = runner.owned_descriptor(path, append=True)
    new = runner.owned_descriptor(path, append=True)
    try:
        os.write(old, b"old running\n")
        os.lseek(new, 0, os.SEEK_END)
        os.write(old, b"old shutdown\n")
        os.write(new, b"new startup\n")
        os.write(old, b"old finished\n")
        os.write(new, b"new ready\n")
    finally:
        os.close(old)
        os.close(new)
    assert path.read_text() == (
        "old running\nold shutdown\nnew startup\nold finished\nnew ready\n"
    )


@pytest.mark.parametrize("raw", [b"x" * 16385, b"[]", b"invalid-json"])
def test_status_rejects_oversized_or_malformed_response(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, raw: bytes,
) -> None:
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = raw
    monkeypatch.setattr(runner.urllib.request, "build_opener", lambda *_: opener)
    with pytest.raises(ValueError):
        runner.status("a" * 64)


@pytest.mark.parametrize("secret", ["", "a" * 63, "a" * 65, "g" * 64, "A" * 64])
def test_main_rejects_invalid_secret_before_runtime_creation(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, secret: str,
) -> None:
    monkeypatch.setenv("CODEX_CONTROL_SHARED_SECRET", secret)
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    restart = MagicMock()
    monkeypatch.setattr(runner, "restart", restart)
    assert runner.main() == 78
    assert not (tmp_path / ".admin-runtime").exists()
    restart.assert_not_called()


def test_main_creates_private_runtime_without_changing_legacy_runtime(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    legacy = tmp_path / ".runtime"
    legacy.mkdir(mode=0o755)
    monkeypatch.setenv("CODEX_CONTROL_SHARED_SECRET", "a" * 64)
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    def assert_append(log_fd: int, secret: str) -> None:
        assert fcntl.fcntl(log_fd, fcntl.F_GETFL) & os.O_APPEND

    restart = MagicMock(side_effect=assert_append)
    monkeypatch.setattr(runner, "restart", restart)
    assert runner.main() == 0
    private = tmp_path / ".admin-runtime"
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE((private / "restart.lock").stat().st_mode) == 0o600
    assert stat.S_IMODE((private / "codex-review.log").stat().st_mode) == 0o600
    assert stat.S_IMODE(legacy.stat().st_mode) == 0o755
    restart.assert_called_once()


@pytest.mark.parametrize("unsafe_kind", ["world-readable", "symlink"])
def test_main_rejects_unsafe_private_runtime(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unsafe_kind: str,
) -> None:
    runtime = tmp_path / ".admin-runtime"
    if unsafe_kind == "symlink":
        target = tmp_path / "elsewhere"
        target.mkdir(mode=0o700)
        runtime.symlink_to(target, target_is_directory=True)
    else:
        runtime.mkdir(mode=0o755)
    monkeypatch.setenv("CODEX_CONTROL_SHARED_SECRET", "a" * 64)
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    restart = MagicMock()
    monkeypatch.setattr(runner, "restart", restart)
    assert runner.main() == 78
    restart.assert_not_called()
