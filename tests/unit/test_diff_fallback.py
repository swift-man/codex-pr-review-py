"""Regression coverage for the diff-only fallback path.

시나리오: 전체 코드베이스가 `CODEX_MAX_INPUT_TOKENS` 를 초과해 변경 파일이 누락됐을 때,
서버가 자동으로 unified patch 만 가지고 리뷰를 돌리는 경로가 end-to-end 로 맞는지 확인.

검증 layer:
  1) DiffContextCollector — PR.diff_patches → FileDump(mode="diff") 변환 정확성
  2) codex_prompt.build_prompt — mode 에 따라 시스템 규칙·본문 포맷 분기
  3) ReviewPullRequestUseCase — 전체 수집이 예산 넘으면 diff fallback 으로 전환,
     diff 까지 넘으면 기존 안내 코멘트 게시 유지
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codex_review.application.review_pr_use_case import ReviewPullRequestUseCase
from codex_review.domain import (
    DUMP_MODE_DIFF,
    DUMP_MODE_FULL,
    FileDump,
    FileEntry,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
    TokenBudget,
)
from codex_review.infrastructure.codex_prompt import build_prompt
from codex_review.infrastructure.diff_context_collector import DiffContextCollector


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _pr(
    changed: tuple[str, ...] = ("a.py", "b.py"),
    patches: dict[str, str] | None = None,
    diff_right: dict[str, frozenset[int]] | None = None,
) -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=1,
        title="t",
        body="pr body",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/x.git",
        changed_files=changed,
        installation_id=7,
        is_draft=False,
        diff_right_lines=diff_right or {},
        diff_patches=patches or {},
    )


@dataclass
class _CapturingGitHub:
    posted_reviews: list[tuple[PullRequest, ReviewResult]] = field(default_factory=list)
    posted_comments: list[tuple[PullRequest, str]] = field(default_factory=list)

    async def fetch_pull_request(
        self, repo: RepoRef, number: int, installation_id: int
    ) -> PullRequest:
        raise AssertionError("not used in these tests")

    async def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        self.posted_reviews.append((pr, result))

    async def post_comment(self, pr: PullRequest, body: str) -> None:
        self.posted_comments.append((pr, body))

    async def get_installation_token(self, installation_id: int) -> str:
        return "tkn"


class _NoopFetcher:
    @asynccontextmanager
    async def session(
        self, pr: PullRequest, installation_token: str
    ) -> AsyncIterator[Path]:
        yield Path(".")


@dataclass
class _StaticFullCollector:
    """예산 초과 시나리오를 재현하기 위한 full collector 더블."""

    dump: FileDump

    async def collect(
        self, root: Path, changed_files: tuple[str, ...], budget: TokenBudget
    ) -> FileDump:
        return self.dump


@dataclass
class _CapturingEngine:
    result: ReviewResult
    seen_dumps: list[FileDump] = field(default_factory=list)

    async def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        self.seen_dumps.append(dump)
        return self.result


# ---------------------------------------------------------------------------
# DiffContextCollector
# ---------------------------------------------------------------------------


async def test_diff_collector_builds_dump_from_patches() -> None:
    collector = DiffContextCollector()
    patches = {
        "a.py": "@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n",
        "b.py": "@@ -0,0 +1,1 @@\n+print('hi')\n",
    }
    pr = _pr(changed=("a.py", "b.py"), patches=patches)

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=10_000))

    assert dump.mode == DUMP_MODE_DIFF
    assert len(dump.entries) == 2
    assert dump.entries[0].path == "a.py"
    # entry.content 에 원문 patch 가 들어가고 파일 헤더가 앞에 붙는다.
    assert "=== PATCH: a.py ===" in dump.entries[0].content
    assert "+y = 2" in dump.entries[0].content
    assert dump.entries[1].path == "b.py"
    assert "+print('hi')" in dump.entries[1].content
    assert dump.exceeded_budget is False
    assert dump.patch_missing == ()
    # `budget` 필드가 전달돼야 이후 본문 배지 등에서 한도를 노출할 수 있다.
    assert dump.budget is not None and dump.budget.max_tokens == 10_000


async def test_diff_collector_marks_patch_missing_files() -> None:
    """GitHub 가 patch 를 안 준 파일(rename/delete/binary/거대 diff) 은 patch_missing 에 적재."""
    collector = DiffContextCollector()
    pr = _pr(
        changed=("a.py", "removed.bin", "renamed.jpg"),
        patches={"a.py": "@@ -1,1 +1,1 @@\n-x\n+y\n"},
    )

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=10_000))

    assert [e.path for e in dump.entries] == ["a.py"]
    assert dump.patch_missing == ("removed.bin", "renamed.jpg")
    # excluded 에는 patch_missing 이 그대로 섞여 들어가야 운영자 노출용으로 통합됨.
    assert "removed.bin" in dump.excluded and "renamed.jpg" in dump.excluded
    assert dump.exceeded_budget is False  # patch 누락은 예산 이슈와 구분


async def test_diff_collector_truncates_when_budget_exceeded() -> None:
    """predicted size 가 예산을 넘는 순간 이후 파일은 drop — 부분 patch 로 자르지 않는다."""
    collector = DiffContextCollector()
    # patch 하나당 body ≈ 186 chars (header 20 + patch 내용 166).
    big_patch = "@@ -1,1 +1,1 @@\n" + "+x\n" * 50
    patches = {"a.py": big_patch, "b.py": big_patch, "c.py": big_patch}
    pr = _pr(changed=("a.py", "b.py", "c.py"), patches=patches)

    # chars_per_token=4 기본, max_tokens=100 → 400 chars 한도.
    # a.py(186) + b.py(186) = 372 → 들어가고, c.py(+186) 를 넣으면 558 → 초과.
    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=100))

    assert dump.exceeded_budget is True
    assert [e.path for e in dump.entries] == ["a.py", "b.py"]
    # c.py 는 예산 초과로 제외 (patch_missing 과 구분된 budget_trimmed).
    assert "c.py" in dump.excluded
    assert dump.patch_missing == ()


async def test_diff_collector_truncates_first_oversize_file() -> None:
    """첫 파일이 예산을 단독으로 넘기면 entries 가 비고 exceeded_budget=True."""
    collector = DiffContextCollector()
    huge = "@@ -1,1 +1,1 @@\n" + "+x\n" * 500  # ~1500 chars
    pr = _pr(changed=("big.py",), patches={"big.py": huge})

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=10))  # 40 chars

    assert dump.entries == ()
    assert dump.exceeded_budget is True
    assert "big.py" in dump.excluded


async def test_diff_collector_empty_when_no_patches() -> None:
    collector = DiffContextCollector()
    pr = _pr(changed=("a.py",), patches={})

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=10_000))

    assert dump.entries == ()
    assert dump.patch_missing == ("a.py",)


# ---------------------------------------------------------------------------
# Prompt builder — mode branching
# ---------------------------------------------------------------------------


def test_build_prompt_full_mode_uses_standard_system_rules() -> None:
    """full 모드 프롬프트는 '전체 코드베이스' 리뷰 규칙을 써야 한다."""
    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),),
        total_chars=3,
        mode=DUMP_MODE_FULL,
    )
    prompt = build_prompt(_pr(), dump)

    assert "전체 코드베이스" in prompt
    assert "1-based 줄 번호" in prompt or "NNNNN|" in prompt
    # diff-only 배지는 있으면 안 된다.
    assert "diff-only mode" not in prompt


def test_build_prompt_diff_mode_switches_system_rules() -> None:
    """diff 모드 프롬프트는 '보이지 않는 코드에 대한 추측 금지' 규칙이 들어가야 한다."""
    patch_content = "=== PATCH: a.py ===\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    dump = FileDump(
        entries=(
            FileEntry(path="a.py", content=patch_content, size_bytes=len(patch_content), is_changed=True),
        ),
        total_chars=len(patch_content),
        mode=DUMP_MODE_DIFF,
    )
    prompt = build_prompt(_pr(), dump)

    # 핵심 계약: 보이지 않는 코드 추측 금지 메시지 + diff 해석 가이드가 포함.
    assert "보이지 않는 코드" in prompt
    assert "추측" in prompt
    assert "@@ -a,b +c,d @@" in prompt or "hunk" in prompt.lower() or "@@" in prompt
    # diff 모드 배지
    assert "diff-only" in prompt
    # full 모드의 "전체 코드베이스를 리뷰한다" 라는 **행동 규칙** 문장이 들어오면 안 된다.
    # (diff 모드에선 "전체 코드베이스 컨텍스트가 초과됐다" 는 **설명** 에는 그 단어가 나오므로
    # 구문 자체를 체크한다.)
    assert "전체 코드베이스**를 한국어로 리뷰한다" not in prompt
    assert "**전체 코드베이스**를 한국어로 리뷰한다" not in prompt
    # patch 원문이 그대로 전달됨
    assert "=== PATCH: a.py ===" in prompt
    assert "+new" in prompt


def test_build_prompt_diff_mode_lists_patch_missing_and_trimmed() -> None:
    """diff 모드 SCOPE 섹션에 patch 누락 · 예산 컷 파일이 별도로 노출돼야 한다."""
    dump = FileDump(
        entries=(FileEntry(path="a.py", content="...", size_bytes=3, is_changed=True),),
        total_chars=3,
        excluded=("big.py", "bin.dat", "renamed.jpg"),
        exceeded_budget=True,
        patch_missing=("bin.dat", "renamed.jpg"),
        mode=DUMP_MODE_DIFF,
    )
    prompt = build_prompt(_pr(), dump)

    assert "patch 를 주지 않아" in prompt
    assert "bin.dat" in prompt
    assert "renamed.jpg" in prompt
    assert "예산 초과" in prompt
    assert "big.py" in prompt


# ---------------------------------------------------------------------------
# Use case — automatic fallback behavior
# ---------------------------------------------------------------------------


def _use_case(
    github: _CapturingGitHub,
    full_dump: FileDump,
    engine_result: ReviewResult,
    max_tokens: int = 1000,
    with_diff_collector: bool = True,
) -> tuple[ReviewPullRequestUseCase, _CapturingEngine]:
    engine = _CapturingEngine(result=engine_result)
    uc = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=_NoopFetcher(),
        file_collector=_StaticFullCollector(dump=full_dump),
        engine=engine,
        max_input_tokens=max_tokens,
        diff_context_collector=DiffContextCollector() if with_diff_collector else None,
    )
    return uc, engine


async def test_use_case_falls_back_to_diff_when_full_exceeds_and_changed_missing() -> None:
    """핵심 계약: 변경 파일이 예산 때문에 빠졌을 때 diff fallback 으로 리뷰가 게시돼야 한다."""
    github = _CapturingGitHub()
    # full 수집이 예산 초과 + 변경 파일 중 b.py 가 빠짐.
    exceeded_full = FileDump(
        entries=(FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),),
        total_chars=3,
        excluded=("b.py",),
        exceeded_budget=True,
        mode=DUMP_MODE_FULL,
    )
    engine_result = ReviewResult(summary="OK", event=ReviewEvent.COMMENT)
    uc, engine = _use_case(github, exceeded_full, engine_result)

    patches = {
        "a.py": "@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n",
        "b.py": "@@ -0,0 +1,1 @@\n+print('hi')\n",
    }
    pr = _pr(patches=patches)

    await uc.execute(pr)

    # 리뷰가 정상 게시돼야 하고(코멘트 안내 아님), diff dump 로 엔진이 돌았어야 한다.
    assert github.posted_comments == []
    assert len(github.posted_reviews) == 1
    assert len(engine.seen_dumps) == 1
    assert engine.seen_dumps[0].mode == DUMP_MODE_DIFF

    # 본문 배지가 summary 최상단에 붙어야 리뷰어가 diff-only 임을 인지한다.
    _, posted = github.posted_reviews[0]
    assert "diff-only" in posted.summary
    assert "자동 전환" in posted.summary


async def test_use_case_posts_budget_notice_when_diff_also_fails() -> None:
    """diff fallback 이 불가능한 경우(예: patch 하나도 없음) 기존 안내 경로로 떨어진다."""
    github = _CapturingGitHub()
    exceeded_full = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.py", "b.py"),
        exceeded_budget=True,
        mode=DUMP_MODE_FULL,
    )
    engine_result = ReviewResult(summary="unused", event=ReviewEvent.COMMENT)
    uc, engine = _use_case(github, exceeded_full, engine_result)

    pr = _pr(patches={})  # GitHub 가 patch 를 전혀 안 줌

    await uc.execute(pr)

    assert engine.seen_dumps == []  # 엔진 호출 없어야 함
    assert github.posted_reviews == []
    assert len(github.posted_comments) == 1
    assert "예산 초과" in github.posted_comments[0][1]


async def test_use_case_uses_full_mode_when_budget_fits() -> None:
    """예산 안쪽에서 돌 때는 기존 full 모드 경로를 그대로 타야 한다 (회귀 방지)."""
    github = _CapturingGitHub()
    ok_dump = FileDump(
        entries=(
            FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),
            FileEntry(path="b.py", content="y=2", size_bytes=3, is_changed=True),
        ),
        total_chars=6,
        mode=DUMP_MODE_FULL,
    )
    engine_result = ReviewResult(summary="OK", event=ReviewEvent.COMMENT)
    uc, engine = _use_case(github, ok_dump, engine_result)

    pr = _pr(patches={"a.py": "@@\n+x\n"})  # patches 있어도 fallback 안 타야 한다.

    await uc.execute(pr)

    assert len(engine.seen_dumps) == 1
    assert engine.seen_dumps[0].mode == DUMP_MODE_FULL
    _, posted = github.posted_reviews[0]
    # full 모드 리뷰엔 diff 배지가 없어야 한다.
    assert "diff-only" not in posted.summary


async def test_use_case_fallback_disabled_returns_to_legacy_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """diff_context_collector=None 이면 이전 동작(안내만 게시) 이 그대로 유지된다.
    운영자가 의도적으로 fallback 을 끄고 싶은 경우 대비.
    """
    github = _CapturingGitHub()
    exceeded = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.py",),
        exceeded_budget=True,
        mode=DUMP_MODE_FULL,
    )
    engine_result = ReviewResult(summary="unused", event=ReviewEvent.COMMENT)
    uc, engine = _use_case(
        github, exceeded, engine_result, with_diff_collector=False
    )
    pr = _pr(patches={"a.py": "@@ -1 +1 @@\n-x\n+y\n"})  # 있어도 무시돼야 함

    await uc.execute(pr)

    assert engine.seen_dumps == []
    assert github.posted_reviews == []
    assert len(github.posted_comments) == 1
    assert "예산 초과" in github.posted_comments[0][1]


async def test_use_case_fallback_empty_result_goes_to_budget_notice() -> None:
    """변경 파일이 전부 `patch_missing` 이라 diff dump 가 비면 fallback 이 의미 없다 —
    안내 코멘트 경로로 떨어져야 한다.
    """
    github = _CapturingGitHub()
    exceeded_full = FileDump(
        entries=(),
        total_chars=0,
        excluded=("bin.dat",),
        exceeded_budget=True,
        mode=DUMP_MODE_FULL,
    )
    engine_result = ReviewResult(summary="unused", event=ReviewEvent.COMMENT)
    uc, engine = _use_case(github, exceeded_full, engine_result)

    pr = _pr(
        changed=("bin.dat",),
        patches={},  # patch 누락 — diff 에서도 볼 수 있는 게 없다
    )
    await uc.execute(pr)

    assert engine.seen_dumps == []
    assert len(github.posted_comments) == 1


# ---------------------------------------------------------------------------
# PullRequest.diff_patches immutability (domain 회귀)
# ---------------------------------------------------------------------------


def test_pull_request_diff_patches_is_wrapped_in_mapping_proxy() -> None:
    from types import MappingProxyType

    pr = _pr(patches={"a.py": "@@ -1 +1 @@\n+x\n"})
    assert isinstance(pr.diff_patches, MappingProxyType)
    with pytest.raises(TypeError):
        pr.diff_patches["b.py"] = "nope"  # type: ignore[index]


def test_pull_request_diff_patches_external_mutation_does_not_leak() -> None:
    mutable: dict[str, str] = {"a.py": "patch1"}
    pr = _pr(patches=mutable)
    mutable["b.py"] = "patch2"  # 생성 이후 원본 변경
    assert "b.py" not in pr.diff_patches
