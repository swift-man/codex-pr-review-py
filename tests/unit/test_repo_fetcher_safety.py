"""Regression coverage for GitRepoFetcher concurrency + secret-handling:
  - 같은 저장소에 대한 checkout 이 저장소별 lock 으로 직렬화되는지
  - fetch/checkout 실패 시에도 원격 URL 이 원상 복구되는지 (토큰 누수 방지)
  - 디버그 로그에 토큰이 포함된 URL 이 마스킹되어 기록되는지
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from codex_review.domain import PullRequest, RepoRef
from codex_review.infrastructure import git_repo_fetcher


def _pr(owner: str = "o", name: str = "r", head: str = "abc") -> PullRequest:
    return PullRequest(
        repo=RepoRef(owner, name),
        number=1,
        title="t",
        body="",
        head_sha=head,
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://github.com/o/r.git",
        changed_files=(),
        installation_id=7,
        is_draft=False,
    )


async def test_same_repo_checkouts_are_serialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """같은 저장소 full_name 에 대한 checkout 은 lock 으로 직렬화돼야 한다.
    다른 저장소는 병렬로 진행될 수 있어야 한다.
    """
    in_flight_by_repo: dict[str, int] = {}
    peak_by_repo: dict[str, int] = {}
    release = asyncio.Event()

    async def fake_run(cmd: list[str], *, check: bool = True) -> None:
        # 커맨드 인자 중에 저장소 경로를 포함하는 `-C <path>` 가 있으면 그 경로를 키로,
        # 없으면 clone 케이스이니 두번째 이후 인자에서 path 추출.
        repo_key = _extract_repo_key(cmd, tmp_path)
        in_flight_by_repo[repo_key] = in_flight_by_repo.get(repo_key, 0) + 1
        peak_by_repo[repo_key] = max(
            peak_by_repo.get(repo_key, 0), in_flight_by_repo[repo_key]
        )
        try:
            await release.wait()
        finally:
            in_flight_by_repo[repo_key] -= 1

    monkeypatch.setattr(git_repo_fetcher, "_run", fake_run)

    fetcher = git_repo_fetcher.GitRepoFetcher(cache_dir=tmp_path)

    # 서로 다른 저장소 두 개를 동시에 checkout — 병렬 진행 허용.
    pr_a1 = _pr("acme", "a")
    pr_a2 = _pr("acme", "a", head="def")   # 같은 저장소, 다른 PR
    pr_b = _pr("acme", "b")

    async def gate() -> None:
        for _ in range(200):
            total_in_flight = sum(in_flight_by_repo.values())
            if total_in_flight >= 2:
                break
            await asyncio.sleep(0.005)
        release.set()

    asyncio.create_task(gate())

    # 같은 저장소 두 번 + 다른 저장소 한 번 동시 실행
    await asyncio.gather(
        fetcher.checkout(pr_a1, "tok"),
        fetcher.checkout(pr_a2, "tok"),
        fetcher.checkout(pr_b, "tok"),
    )

    # acme/a 는 직렬 (peak 1), acme/b 는 a 와 병렬이라 전체 가동 중 1 이상만 보장.
    a_peak = max(v for k, v in peak_by_repo.items() if "/acme/a/" in k or k.endswith("/acme/a"))
    assert a_peak == 1, "같은 저장소의 checkout 은 직렬화돼야 한다"


async def test_remote_url_is_restored_even_when_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fetch/checkout 단계에서 예외가 나도 `remote set-url ... <original>` 이 호출돼
    토큰이 `.git/config` 에 남지 않아야 한다.
    """
    restore_calls: list[list[str]] = []

    async def fake_run(cmd: list[str], *, check: bool = True) -> None:
        # "fetch" 에서 강제 실패
        if "fetch" in cmd:
            raise RuntimeError("boom")
        if cmd[-2:-1] == ["origin"] and "set-url" in cmd and not check:
            # 복구 호출: check=False 로 넘어오는 마지막 set-url
            restore_calls.append(cmd)

    monkeypatch.setattr(git_repo_fetcher, "_run", fake_run)
    # .git 디렉터리가 있는 것처럼 위장 → clone 은 skip, set-url 분기로
    repo_dir = tmp_path / "acme" / "a" / ".git"
    repo_dir.mkdir(parents=True)

    fetcher = git_repo_fetcher.GitRepoFetcher(cache_dir=tmp_path)

    with pytest.raises(RuntimeError, match="boom"):
        await fetcher.checkout(_pr("acme", "a"), "secret-token")

    # 실패 후에도 반드시 원래 URL 로 복구된 호출이 있어야 한다.
    assert restore_calls, "fetch 실패 후에도 remote URL 복구가 호출돼야 한다"
    restored_url = restore_calls[-1][-1]
    assert "secret-token" not in restored_url, "복구된 URL 엔 토큰이 없어야 한다"
    assert restored_url == "https://github.com/o/r.git"


def test_mask_token_in_url_strips_userinfo() -> None:
    masked = git_repo_fetcher._mask_token_in_url(
        "https://x-access-token:SECRET123@github.com/o/r.git"
    )
    assert "SECRET123" not in masked
    assert masked.startswith("https://***@github.com")
    # 비 URL 문자열은 건드리지 않는다.
    assert git_repo_fetcher._mask_token_in_url("--force") == "--force"
    assert git_repo_fetcher._mask_token_in_url("/tmp/repo") == "/tmp/repo"


async def test_debug_log_does_not_leak_token(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """디버그 로그에 토큰이 포함된 clone_url 이 원문으로 기록되지 않아야 한다."""
    # 프로세스는 실제로 실행하지 않고 subprocess 를 가로챈다.

    class _DummyProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_create(*_args: Any, **_kwargs: Any) -> _DummyProc:
        return _DummyProc()

    monkeypatch.setattr(
        "codex_review.infrastructure.git_repo_fetcher.asyncio.create_subprocess_exec",
        fake_create,
    )

    with caplog.at_level(logging.DEBUG, logger="codex_review.infrastructure.git_repo_fetcher"):
        await git_repo_fetcher._run([
            "git", "clone", "--filter=blob:none",
            "https://x-access-token:TOKEN123@github.com/o/r.git", "/tmp/r",
        ])

    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "TOKEN123" not in all_text
    assert "***@github.com" in all_text


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _extract_repo_key(cmd: list[str], cache_root: Path) -> str:
    for i, a in enumerate(cmd):
        if a == "-C" and i + 1 < len(cmd):
            return cmd[i + 1]
    # clone 케이스: 마지막 인자가 target path
    return cmd[-1]
