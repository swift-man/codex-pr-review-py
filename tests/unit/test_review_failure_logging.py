import logging
from unittest.mock import AsyncMock, Mock

import pytest

from codex_review.application.review_pr_use_case import ReviewPullRequestUseCase
from codex_review.domain import FileDump, PullRequest, RepoRef
from codex_review.interfaces import ReviewEngineError
from codex_review.logging_utils import _RedactFilter


@pytest.mark.parametrize("mode", ["preemptive", "full-only", "full-then-diff"])
@pytest.mark.parametrize("filter_installed", [False, True])
async def test_engine_failure_logs_do_not_render_secret_bearing_tracebacks(
    mode: str, filter_installed: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail(*args: object, **kwargs: object) -> None:
        try:
            raise RuntimeError("authorization=Bearer inner-private-token")
        except RuntimeError as cause:
            raise ReviewEngineError(
                "engine rejected https://user:outer-private-token@host/repo secret=private-value"
            ) from cause

    engine = Mock(review=AsyncMock(side_effect=fail))
    uc = ReviewPullRequestUseCase(
        github=Mock(), repo_fetcher=Mock(), file_collector=Mock(),
        engine=engine, max_input_tokens=10_000,
    )
    uc._post_engine_failure_comment = AsyncMock()
    fallback = FileDump(entries=(), total_chars=0, mode="diff")
    uc._try_diff_fallback = AsyncMock(
        return_value=None if mode == "full-only" else fallback,
    )
    pr = Mock(spec=PullRequest, repo=RepoRef("owner", "repo"), number=1)
    dump = FileDump(entries=(), total_chars=0, mode="diff" if mode == "preemptive" else "full")
    redaction = _RedactFilter()
    if filter_installed:
        caplog.handler.addFilter(redaction)
    try:
        with caplog.at_level(logging.WARNING):
            result = await uc._review_with_fallback(
                pr, dump, entered_diff_preemptively=mode == "preemptive", history=None,
            )
    finally:
        caplog.handler.removeFilter(redaction)
    assert result is None
    assert "engine rejected" in caplog.text
    for secret in ("inner-private-token", "outer-private-token", "private-value"):
        assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    uc._post_engine_failure_comment.assert_awaited_once()
