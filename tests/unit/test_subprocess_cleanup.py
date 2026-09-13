"""Regression coverage for `_subprocess.kill_and_reap`:
  - `wait()` 가 느려도 상한(timeout) 안에서 반환한다 (codex 리뷰 지적).
  - 이미 종료된 프로세스(`kill()` → `ProcessLookupError`) 도 안전하게 처리한다.
  - 정상 종료된 프로세스엔 경고를 남기지 않는다.
"""

import asyncio
import logging
import sys
from pathlib import Path

import pytest

from codex_review.infrastructure import _subprocess


class _FakeProc:
    """`asyncio.subprocess.Process` duck type — 테스트 제어용."""

    def __init__(
        self,
        *,
        wait_delay: float = 0.0,
        kill_raises: type[BaseException] | None = None,
        pid: int | None = 1234,
    ) -> None:
        self._wait_delay = wait_delay
        self._kill_raises = kill_raises
        self.kill_called = 0
        self.pid = pid

    def kill(self) -> None:
        self.kill_called += 1
        if self._kill_raises is not None:
            raise self._kill_raises()

    async def wait(self) -> int:
        if self._wait_delay:
            await asyncio.sleep(self._wait_delay)
        return -9


async def test_kill_and_reap_returns_promptly_when_wait_is_fast() -> None:
    proc = _FakeProc(wait_delay=0.0)
    # 기본 타임아웃(5s) 한참 안쪽에서 끝나야 한다.
    await asyncio.wait_for(_subprocess.kill_and_reap(proc), timeout=1.0)
    assert proc.kill_called == 1


async def test_kill_and_reap_enforces_timeout_when_wait_hangs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """회귀(codex inline codex_cli_engine.py:54,:100): `wait()` 가 지연돼도 호출자가
    상한 안에서 돌아와야 한다 — preflight 기동이나 워커가 무한히 잡히지 않도록.
    """
    proc = _FakeProc(wait_delay=10.0)  # wait 가 10s 걸린다 가정.
    with caplog.at_level(logging.WARNING, logger="codex_review.infrastructure._subprocess"):
        # timeout=0.05 로 줄여 빠르게 검증.
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(
            _subprocess.kill_and_reap(proc, timeout=0.05), timeout=1.0
        )
        elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.5, f"kill_and_reap 이 {elapsed:.2f}s 걸림 — 상한이 무시됐을 가능성"
    assert any("did not reap" in rec.getMessage() for rec in caplog.records), (
        "수거 타임아웃은 경고 로그를 남겨 운영자가 프로세스 누수를 탐지할 수 있어야 한다"
    )


async def test_kill_and_reap_tolerates_already_exited_process() -> None:
    """race: 우리가 kill 을 부르기 전에 프로세스가 정상 종료 → `ProcessLookupError`.
    이 경우에도 조용히 reap 만 수행하고 예외는 전파하지 않아야 한다.
    """
    proc = _FakeProc(wait_delay=0.0, kill_raises=ProcessLookupError)
    await asyncio.wait_for(_subprocess.kill_and_reap(proc), timeout=1.0)
    assert proc.kill_called == 1  # kill 호출 자체는 있었음 (실제 예외 발생 후 suppress)


async def test_kill_and_reap_propagates_cancellation() -> None:
    """cleanup 경로 자체가 취소되면 더 기다리지 않고 취소를 전파해야 한다 — 서버 종료가
    cleanup 때문에 막히는 일이 없도록.
    """
    proc = _FakeProc(wait_delay=10.0)
    task = asyncio.create_task(_subprocess.kill_and_reap(proc, timeout=5.0))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_kill_and_reap_falls_back_to_direct_kill_when_group_kill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _FakeProc()

    def fail_killpg(_pid: int, _sig: int) -> None:
        raise OSError("process groups are unavailable")

    monkeypatch.setattr(_subprocess.os, "killpg", fail_killpg)
    await _subprocess.kill_and_reap(proc, process_group=True)

    assert proc.kill_called == 1


@pytest.mark.parametrize("pid", [None, 0, -1])
async def test_kill_and_reap_never_targets_an_invalid_process_group_pid(
    pid: int | None,
) -> None:
    proc = _FakeProc(pid=pid)

    await _subprocess.kill_and_reap(proc, process_group=True)

    assert proc.kill_called == 1


async def test_kill_and_reap_can_terminate_the_entire_cli_process_group(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "descendant-survived"
    started_marker = tmp_path / "descendant-started"
    child = (
        f"import time; open({str(started_marker)!r}, 'w').close(); "
        f"time.sleep(0.8); open({str(marker)!r}, 'w').close()"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    async with asyncio.timeout(2.0):
        while not started_marker.exists():
            await asyncio.sleep(0.01)
    await _subprocess.kill_and_reap(proc, timeout=1.0, process_group=True)
    await asyncio.sleep(1.0)
    assert not marker.exists()
