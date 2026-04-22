import asyncio
import hashlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codex_review.application.review_pr_use_case import ReviewPullRequestUseCase
from codex_review.application.webhook_handler import WebhookHandler
from codex_review.domain import (
    FileDump,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
    TokenBudget,
)

SECRET = "top-secret"


@dataclass
class FakeGitHub:
    posted_reviews: list[tuple[PullRequest, ReviewResult]] = field(default_factory=list)
    posted_comments: list[tuple[PullRequest, str]] = field(default_factory=list)
    pr_to_return: PullRequest | None = None

    async def fetch_pull_request(
        self, repo: RepoRef, number: int, installation_id: int
    ) -> PullRequest:
        assert self.pr_to_return is not None
        return self.pr_to_return

    async def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        self.posted_reviews.append((pr, result))

    async def post_comment(self, pr: PullRequest, body: str) -> None:
        self.posted_comments.append((pr, body))

    async def get_installation_token(self, installation_id: int) -> str:
        return "fake-token"


@dataclass
class FakeFetcher:
    path: Path

    @asynccontextmanager
    async def session(
        self, pr: PullRequest, installation_token: str
    ) -> AsyncIterator[Path]:
        yield self.path


class FakeCollector:
    def __init__(self, dump: FileDump) -> None:
        self._dump = dump

    async def collect(
        self, root: Path, changed_files: tuple[str, ...], budget: TokenBudget
    ) -> FileDump:
        return self._dump


class FakeEngine:
    def __init__(self, result: ReviewResult) -> None:
        self._result = result

    async def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        return self._result


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _sample_pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=1,
        title="t",
        body="",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/x.git",
        changed_files=("a.py",),
        installation_id=7,
        is_draft=False,
    )


def _build_handler(
    github: FakeGitHub,
    dump: FileDump,
    result: ReviewResult,
    tmp: Path,
    concurrency: int = 1,
) -> WebhookHandler:
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp),
        file_collector=FakeCollector(dump),
        engine=FakeEngine(result),
        max_input_tokens=1000,
    )
    return WebhookHandler(
        secret=SECRET, github=github, use_case=use_case, concurrency=concurrency
    )


def test_verify_signature_accepts_valid_and_rejects_invalid(tmp_path: Path) -> None:
    dump = FileDump(entries=(), total_chars=0)
    result = ReviewResult(summary="ok", event=ReviewEvent.COMMENT)
    handler = _build_handler(FakeGitHub(), dump, result, tmp_path)

    body = b'{"a":1}'
    assert handler.verify_signature(_sign(body), body) is True
    assert handler.verify_signature("sha256=wrong", body) is False
    assert handler.verify_signature(None, body) is False


async def test_accept_ignores_non_pr_events(tmp_path: Path) -> None:
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    code, _ = await handler.accept("issues", "d1", {})
    assert code == 202


async def test_accept_ignores_draft(tmp_path: Path) -> None:
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"draft": True, "number": 1},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    code, reason = await handler.accept("pull_request", "d2", payload)
    assert code == 202
    assert reason == "skipped-draft"


async def test_accept_ignores_unsupported_action(tmp_path: Path) -> None:
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "closed",
        "pull_request": {"number": 1},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    code, _ = await handler.accept("pull_request", "d3", payload)
    assert code == 202


async def test_use_case_posts_comment_when_budget_exceeded(tmp_path: Path) -> None:
    github = FakeGitHub()
    pr = _sample_pr()
    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.py",),
        exceeded_budget=True,
        budget=TokenBudget(1),
    )
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=FakeEngine(ReviewResult(summary="x", event=ReviewEvent.COMMENT)),
        max_input_tokens=1,
    )

    await use_case.execute(pr)

    assert github.posted_reviews == []
    assert len(github.posted_comments) == 1
    assert "예산 초과" in github.posted_comments[0][1]


async def test_use_case_posts_review_when_budget_fits(tmp_path: Path) -> None:
    github = FakeGitHub()
    pr = _sample_pr()
    from codex_review.domain import FileEntry

    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),),
        total_chars=3,
        exceeded_budget=False,
    )
    expected = ReviewResult(summary="good", event=ReviewEvent.COMMENT)
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=FakeEngine(expected),
        max_input_tokens=1000,
    )

    await use_case.execute(pr)

    assert github.posted_comments == []
    assert len(github.posted_reviews) == 1
    assert github.posted_reviews[0][1] is expected


async def test_accept_queues_valid_pr_and_returns_202(tmp_path: Path) -> None:
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"draft": False, "number": 42},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    code, reason = await handler.accept("pull_request", "d4", payload)
    assert code == 202
    assert reason == "queued"


# ---------------------------------------------------------------------------
# Concurrency behavior: Semaphore(N) 이 실제로 동시 실행 상한을 지키는지
# ---------------------------------------------------------------------------


class _SlowEngine:
    """review() 를 두 단계로 나눈 엔진 — 테스트가 '지금 몇 개가 병렬 실행 중인지' 관찰 가능."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0
        self.release = asyncio.Event()

    async def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await self.release.wait()
            return ReviewResult(summary="done", event=ReviewEvent.COMMENT)
        finally:
            self.in_flight -= 1


async def _queue_two_prs(handler: WebhookHandler) -> None:
    for n in (1, 2):
        await handler.accept(
            "pull_request",
            f"d-{n}",
            {
                "action": "opened",
                "pull_request": {"draft": False, "number": n},
                "repository": {"full_name": "o/r"},
                "installation": {"id": 7},
            },
        )


async def _run_handler_with_engine(engine: _SlowEngine, concurrency: int) -> None:
    github = FakeGitHub(pr_to_return=_sample_pr())
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(Path(".")),
        file_collector=FakeCollector(FileDump(entries=(), total_chars=0)),
        engine=engine,
        max_input_tokens=1000,
    )
    handler = WebhookHandler(
        secret=SECRET, github=github, use_case=use_case, concurrency=concurrency
    )
    await handler.start()
    try:
        await _queue_two_prs(handler)
        # 워커가 작업을 picking up 할 때까지 잠시 대기 (공평한 스케줄링 허용).
        for _ in range(100):
            if engine.in_flight > 0:
                break
            await asyncio.sleep(0.01)
        # 한 번 더 양보해 두 번째 워커도 진입할 기회를 준다.
        await asyncio.sleep(0.05)
        engine.release.set()
        # 큐 소진 대기.
        await asyncio.wait_for(handler._queue.join(), timeout=2.0)  # type: ignore[attr-defined]
    finally:
        await handler.stop()


async def test_concurrency_1_runs_one_at_a_time() -> None:
    engine = _SlowEngine()
    await _run_handler_with_engine(engine, concurrency=1)
    assert engine.peak == 1


async def test_concurrency_2_runs_two_in_parallel() -> None:
    engine = _SlowEngine()
    await _run_handler_with_engine(engine, concurrency=2)
    assert engine.peak == 2
