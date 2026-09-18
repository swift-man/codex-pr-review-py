import asyncio
from typing import Any

import pytest

from codex_review.application.review_pr_use_case import _is_model_limit_error
from codex_review.domain import (
    DUMP_MODE_DIFF,
    FileDump,
    FileEntry,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
)
from codex_review.infrastructure.codex_cli_engine import (
    CODEX_CLI_MAX_INPUT_CHARS,
    CodexAuthError,
    CodexCliEngine,
)
from codex_review.infrastructure.file_dump_collector import (
    estimate_full_prompt_file_chars,
)
from codex_review.interfaces import ReviewEngineError
from codex_review.model_utils import ReasoningEffort


class _FakeProc:
    def __init__(
        self,
        returncode: int,
        stdout: bytes = b"",
        stderr: bytes = b"",
        *,
        pid: int | None = None,
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.pid = pid

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    result: Any,
    captured_kwargs: list[dict[str, Any]] | None = None,
) -> None:
    async def fake_create(*_args: Any, **kwargs: Any) -> Any:
        if captured_kwargs is not None:
            captured_kwargs.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        "codex_review.infrastructure.codex_cli_engine.asyncio.create_subprocess_exec",
        fake_create,
    )


def _patch_subprocess_sequence(
    monkeypatch: pytest.MonkeyPatch,
    results: list[Any],
    calls: list[tuple[Any, ...]],
) -> None:
    async def fake_create(*args: Any, **_kwargs: Any) -> Any:
        calls.append(args)
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        "codex_review.infrastructure.codex_cli_engine.asyncio.create_subprocess_exec",
        fake_create,
    )


def _engine() -> CodexCliEngine:
    return CodexCliEngine(binary="codex", model="gpt-5.4")


def _sample_review_input() -> tuple[PullRequest, FileDump]:
    pr = PullRequest(
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
    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),),
        total_chars=3,
    )
    return pr, dump


async def test_review_rejects_prompt_over_cli_limit_before_starting_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr, dump = _sample_review_input()
    oversized_prompt = "x" * (CODEX_CLI_MAX_INPUT_CHARS + 1)
    monkeypatch.setattr(
        "codex_review.infrastructure.codex_cli_engine.build_prompt",
        lambda *_args, **_kwargs: oversized_prompt,
    )
    _patch_subprocess(monkeypatch, AssertionError("subprocess must not start"))

    with pytest.raises(ReviewEngineError, match=r"actual_chars=1048577"):
        await _engine().review(pr, dump)


async def test_verify_auth_passes_when_logged_in_on_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(monkeypatch, _FakeProc(0, stdout=b"Logged in using ChatGPT\n"))
    assert (await _engine().verify_auth()).startswith("Logged in")


async def test_verify_auth_passes_when_logged_in_on_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """codex CLI 는 non-TTY 환경에서 상태를 stderr 로 보낸다."""
    _patch_subprocess(monkeypatch, _FakeProc(0, stderr=b"Logged in using ChatGPT\n"))
    assert (await _engine().verify_auth()).startswith("Logged in")


async def test_verify_auth_raises_when_not_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    group_kills: list[int] = []

    class _FailedAuthProc(_FakeProc):
        def kill(self) -> None:
            events.append("kill")

        async def wait(self) -> int:
            events.append("wait")
            return -9

    monkeypatch.setattr(
        "codex_review.infrastructure._subprocess.os.killpg",
        lambda pid, _sig: group_kills.append(pid),
    )
    _patch_subprocess(
        monkeypatch,
        _FailedAuthProc(1, stderr=b"Not logged in", pid=1234),
    )
    with pytest.raises(CodexAuthError) as exc:
        await _engine().verify_auth()
    assert "codex login" in str(exc.value)
    # 그룹 종료와 별개로 직접 자식 종료도 항상 보장한다 — 래퍼가 그룹 밖으로 빠져나간
    # 경우에도 확실히 죽이기 위해서다.
    assert events == ["kill", "wait"]
    assert group_kills == [1234]


async def test_verify_auth_raises_on_unexpected_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(monkeypatch, _FakeProc(0, stdout=b"Some unrelated output\n"))
    with pytest.raises(CodexAuthError):
        await _engine().verify_auth()


async def test_auth_and_review_start_in_new_process_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_kwargs: list[dict[str, Any]] = []
    _patch_subprocess(
        monkeypatch,
        _FakeProc(0, stdout=b"Logged in using ChatGPT\n"),
        auth_kwargs,
    )
    await _engine().verify_auth()

    pr, dump = _sample_review_input()
    review_kwargs: list[dict[str, Any]] = []
    _patch_subprocess(
        monkeypatch,
        _FakeProc(0, stdout=b'{"summary":"ok","event":"COMMENT","comments":[]}\n'),
        review_kwargs,
    )
    await _engine().review(pr, dump)

    assert auth_kwargs[0]["start_new_session"] is True
    assert review_kwargs[0]["start_new_session"] is True


async def test_verify_auth_raises_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(monkeypatch, FileNotFoundError("codex: not found"))
    with pytest.raises(CodexAuthError) as exc:
        await _engine().verify_auth()
    assert "CODEX_BIN" in str(exc.value)


async def test_verify_auth_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TimingOutProc(_FakeProc):
        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            # `asyncio.timeout` 컨텍스트 매니저 안에서 `communicate` 가 느려 TimeoutError 가
            # 발생하는 상황을 직접 재현 — 예외 자체를 던져 같은 경로를 타게 한다.
            raise TimeoutError()

    _patch_subprocess(monkeypatch, _TimingOutProc(0))

    with pytest.raises(CodexAuthError) as exc:
        await _engine().verify_auth()
    assert "10초" in str(exc.value)


async def test_verify_auth_kills_subprocess_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """서버 종료/워커 취소로 `CancelledError` 가 전파되면 하위 프로세스가 좀비로 남지 않도록
    반드시 kill 되고 취소는 재전파돼야 한다.
    """
    events: list[str] = []

    class _CancelledInCommunicate(_FakeProc):
        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            raise asyncio.CancelledError()

        def kill(self) -> None:
            events.append("kill")

        async def wait(self) -> int:
            events.append("wait")
            return -9

    _patch_subprocess(monkeypatch, _CancelledInCommunicate(0))

    with pytest.raises(asyncio.CancelledError):
        await _engine().verify_auth()

    assert events == ["kill", "wait"], "취소 시 kill → wait 순으로 정리돼야 한다"


async def test_verify_auth_kills_subprocess_on_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _UnexpectedErrorProc(_FakeProc):
        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            raise MemoryError("simulated allocation failure")

        def kill(self) -> None:
            events.append("kill")

        async def wait(self) -> int:
            events.append("wait")
            return -9

    _patch_subprocess(monkeypatch, _UnexpectedErrorProc(0))

    with pytest.raises(MemoryError):
        await _engine().verify_auth()

    assert events == ["kill", "wait"]


# ---------------------------------------------------------------------------
# review() error logging contract — full stderr to logger, concise summary in exception
# ---------------------------------------------------------------------------


async def test_review_logs_full_stderr_and_raises_concise_summary(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """회귀 (운영 사고): 이전 구현은 stderr 를 RuntimeError 메시지에 통째로 박아
    traceback summary 가 첫 줄(=Codex 시작 배너)만 보여 진단을 못 하게 했다.
    이제는 stderr 전체를 별도 ERROR 로그에, RuntimeError 에는 마지막 줄만.
    """
    import logging as _logging

    from codex_review.domain import FileDump, FileEntry, PullRequest, RepoRef

    multi_line_stderr = (
        b"OpenAI Codex v0.124.0-alpha.2 (research preview)\n"
        b"--------\n"
        b"workdir: /tmp\n"
        b"model: gpt-5.5\n"
        b"--------\n"
        b"Error: model 'gpt-5.5' not available in this account\n"
    )
    _patch_subprocess(monkeypatch, _FakeProc(1, stdout=b"", stderr=multi_line_stderr))

    pr = PullRequest(
        repo=RepoRef("o", "r"), number=1, title="t", body="",
        head_sha="abc", head_ref="feat", base_sha="def", base_ref="main",
        clone_url="https://example/x.git", changed_files=("a.py",),
        installation_id=7, is_draft=False,
    )
    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),),
        total_chars=3,
    )

    eng = CodexCliEngine(binary="codex", model="gpt-5.5")

    with (
        caplog.at_level(
            _logging.ERROR, logger="codex_review.infrastructure.codex_cli_engine"
        ),
        pytest.raises(RuntimeError) as exc_info,
    ):
        await eng.review(pr, dump)

    # (1) RuntimeError 메시지엔 stderr 의 **마지막 줄** + 모델명이 포함돼 진단 가능.
    msg = str(exc_info.value)
    assert "gpt-5.5" in msg
    assert "model 'gpt-5.5' not available" in msg
    # 시작 배너는 메시지에 없음 (이전엔 첫 줄로 잘려 진단 어려웠음).
    assert "research preview" not in msg

    # (2) ERROR 로그에는 multi-line stderr 전체가 보존됨.
    full_log = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "research preview" in full_log
    assert "model 'gpt-5.5' not available" in full_log
    assert "rc=1" in full_log
    assert "model=gpt-5.5" in full_log


async def test_review_kills_subprocess_before_raising_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    group_kills: list[int] = []

    class _FailedReviewProc(_FakeProc):
        def kill(self) -> None:
            events.append("kill")

        async def wait(self) -> int:
            events.append("wait")
            return -9

    _patch_subprocess(
        monkeypatch,
        _FailedReviewProc(1, stderr=b"Error: model unavailable\n", pid=1234),
    )
    monkeypatch.setattr(
        "codex_review.infrastructure._subprocess.os.killpg",
        lambda pid, _sig: group_kills.append(pid),
    )
    pr, dump = _sample_review_input()

    with pytest.raises(ReviewEngineError):
        await CodexCliEngine(binary="codex", model="gpt-5.5").review(pr, dump)

    assert events == ["kill", "wait"]
    assert group_kills == [1234]


async def test_review_cleans_up_the_process_group_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """정상 종료 경로도 세션을 정리해야 한다.

    래퍼가 종료 코드 0 으로 끝나도 세션에 남은 네이티브 자식은 고아다. 실패 분기만
    정리하면 정상 종료가 대부분인 운영에서 오히려 더 많이 샌다.
    """
    group_kills: list[int] = []

    class _OkProc(_FakeProc):
        async def wait(self) -> int:
            return 0

    monkeypatch.setattr(
        "codex_review.infrastructure._subprocess.os.killpg",
        lambda pid, _sig: group_kills.append(pid),
    )
    _patch_subprocess(
        monkeypatch,
        _OkProc(0, stdout=b'{"summary":"ok","event":"COMMENT","comments":[]}\n', pid=4321),
    )
    pr, dump = _sample_review_input()

    result = await CodexCliEngine(binary="codex", model="gpt-5.5").review(pr, dump)

    assert result.summary == "ok"
    assert group_kills == [4321]


async def test_verify_auth_cleans_up_the_process_group_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_kills: list[int] = []

    class _OkProc(_FakeProc):
        async def wait(self) -> int:
            return 0

    monkeypatch.setattr(
        "codex_review.infrastructure._subprocess.os.killpg",
        lambda pid, _sig: group_kills.append(pid),
    )
    _patch_subprocess(
        monkeypatch, _OkProc(0, stdout=b"Logged in using ChatGPT\n", pid=4321)
    )

    assert "Logged in" in await _engine().verify_auth()
    assert group_kills == [4321]


async def test_review_kills_subprocess_on_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _UnexpectedErrorProc(_FakeProc):
        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            raise RuntimeError("simulated communicate failure")

        def kill(self) -> None:
            events.append("kill")

        async def wait(self) -> int:
            events.append("wait")
            return -9

    _patch_subprocess(monkeypatch, _UnexpectedErrorProc(0))
    pr, dump = _sample_review_input()

    with pytest.raises(RuntimeError):
        await CodexCliEngine(binary="codex", model="gpt-5.5").review(pr, dump)

    assert events == ["kill", "wait"]


async def test_review_tries_fallback_model_after_primary_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    stdout = b'{"summary":"ok","event":"COMMENT","comments":[]}\n'
    _patch_subprocess_sequence(
        monkeypatch,
        [
            _FakeProc(1, stdout=b"", stderr=b"Error: model not available\n"),
            _FakeProc(0, stdout=stdout, stderr=b""),
        ],
        calls,
    )

    pr, dump = _sample_review_input()
    eng = CodexCliEngine(
        binary="codex",
        model="gpt-5.3-codex-spark",
        fallback_models=("gpt-5.5",),
    )

    result = await eng.review(pr, dump)

    assert result.summary == "ok"
    assert result.model_used == "gpt-5.5"
    assert result.reasoning_effort_used == "xhigh"
    assert [call[call.index("--model") + 1] for call in calls] == [
        "gpt-5.3-codex-spark",
        "gpt-5.5",
    ]


async def test_fallback_model_receives_snapshot_trimmed_to_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[tuple[str, int]] = []
    pr, _ = _sample_review_input()
    dump = FileDump(
        entries=(
            FileEntry(path="a.py", content="a" * 150, size_bytes=150, is_changed=True),
            FileEntry(path="b.py", content="b" * 150, size_bytes=150, is_changed=False),
        ),
        total_chars=300,
    )

    monkeypatch.setattr(
        "codex_review.infrastructure.codex_cli_engine.build_prompt",
        lambda _pr, candidate, **_kwargs: "x" * (
            20 + sum(item.size_bytes for item in candidate.entries)
        ),
    )

    async def fake_review(
        _prompt: str,
        candidate: FileDump,
        *,
        model: str,
        timeout_sec: float,
    ) -> ReviewResult:
        attempts.append((model, len(candidate.entries)))
        if model == "primary":
            raise ReviewEngineError("primary unavailable")
        return ReviewResult(summary="ok", event=ReviewEvent.COMMENT)

    engine = CodexCliEngine(
        binary="codex",
        model="primary",
        fallback_models=("fallback",),
        model_input_budgets={"primary": 100, "fallback": 50},
    )
    engine._review_with_model = fake_review  # type: ignore[method-assign]

    result = await engine.review(pr, dump)

    assert result.summary == "ok"
    assert attempts == [("primary", 2), ("fallback", 1)]


def test_model_budget_does_not_drop_changed_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr, _ = _sample_review_input()
    dump = FileDump(
        entries=(
            FileEntry(path="changed.py", content="가" * 150, size_bytes=450, is_changed=True),
            FileEntry(path="context.py", content="b" * 150, size_bytes=150, is_changed=False),
        ),
        total_chars=300,
    )
    monkeypatch.setattr(
        "codex_review.infrastructure.codex_cli_engine.build_prompt",
        lambda _pr, candidate, **_kwargs: "x" * (
            20 + sum(len(item.content) for item in candidate.entries)
        ),
    )

    engine = CodexCliEngine(
        binary="codex",
        model="primary",
        model_input_budgets={"primary": 50},
    )

    candidate = engine._dump_for_model(pr, dump, "primary", history=None)

    assert [entry.path for entry in candidate.entries] == ["changed.py"]
    assert candidate.budget_trimmed == ("context.py",)
    # total_chars 는 FileDumpCollector 와 같은 단위(프롬프트 문자 추정치) 를 유지해야
    # "invoking codex: chars=..." 로그가 축소 여부에 따라 단위를 바꾸지 않는다.
    assert candidate.total_chars == estimate_full_prompt_file_chars(
        "changed.py", "가" * 150, is_changed=True
    )


def test_last_model_is_attempted_even_when_budget_cannot_be_met() -> None:
    """diff 모드 dump 는 전 항목이 변경 파일이라 잘라낼 게 없다.

    마지막 모델까지 예산을 이유로 건너뛰면 리뷰 경로가 통째로 사라지므로, 남은 모델이
    없을 때는 우리 추정 예산을 넘겨도 실제 호출을 시도해야 한다.
    """
    pr, _ = _sample_review_input()
    diff_dump = FileDump(
        entries=(
            FileEntry(path="a.py", content="x" * 600_000, size_bytes=600_000, is_changed=True),
        ),
        total_chars=600_000,
        mode=DUMP_MODE_DIFF,
    )
    engine = CodexCliEngine(
        binary="codex",
        model="primary",
        fallback_models=("last",),
        model_input_budgets={"primary": 121_600, "last": 121_600},
    )

    with pytest.raises(ReviewEngineError, match="token limit"):
        engine._dump_for_model(pr, diff_dump, "primary", history=None, allow_skip=True)

    candidate = engine._dump_for_model(pr, diff_dump, "last", history=None, allow_skip=False)
    assert [entry.path for entry in candidate.entries] == ["a.py"]


def test_budget_skip_error_is_classified_as_a_model_limit_failure() -> None:
    """건너뛰기 사유 메시지는 use case 의 모델 한도 분류에 걸려야 한다.

    분류에 실패하면 head_sha 중복 방지를 타지 못해 같은 실패 코멘트가 재전달마다 쌓인다.
    """
    pr, _ = _sample_review_input()
    dump = FileDump(
        entries=(
            FileEntry(path="a.py", content="x" * 600_000, size_bytes=600_000, is_changed=True),
        ),
        total_chars=600_000,
        mode=DUMP_MODE_DIFF,
    )
    engine = CodexCliEngine(binary="codex", model="m", model_input_budgets={"m": 1_000})

    with pytest.raises(ReviewEngineError) as excinfo:
        engine._dump_for_model(pr, dump, "m", history=None, allow_skip=True)

    assert _is_model_limit_error(excinfo.value)


async def test_review_tries_reserve_then_spark_when_model_limits_are_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    stdout = b'{"summary":"ok","event":"COMMENT","comments":[]}\n'
    _patch_subprocess_sequence(
        monkeypatch,
        [
            _FakeProc(1, stderr=b"Error: gpt-5.6-sol usage limit reached\n"),
            _FakeProc(1, stderr=b"Error: gpt-reserve usage limit reached\n"),
            _FakeProc(0, stdout=stdout),
        ],
        calls,
    )

    pr, dump = _sample_review_input()
    eng = CodexCliEngine(
        binary="codex",
        model="gpt-5.6-sol",
        fallback_models=("gpt-reserve", "gpt-5.3-codex-spark"),
        primary_context_window=872_000,
    )

    result = await eng.review(pr, dump)

    assert result.summary == "ok"
    assert result.model_used == "gpt-5.3-codex-spark"
    assert result.reasoning_effort_used == "xhigh"
    assert [call[call.index("--model") + 1] for call in calls] == [
        "gpt-5.6-sol",
        "gpt-reserve",
        "gpt-5.3-codex-spark",
    ]
    assert "model_context_window=872000" in calls[0]
    assert all("model_context_window=872000" not in call for call in calls[1:])


@pytest.mark.parametrize("primary_effort", ["max", "xhigh"])
@pytest.mark.parametrize("fallback_model", ["gpt-reserve", "gpt-5.6-luna"])
async def test_review_uses_max_for_reserve_and_supported_xhigh_for_spark(
    monkeypatch: pytest.MonkeyPatch,
    primary_effort: ReasoningEffort,
    fallback_model: str,
) -> None:
    calls: list[tuple[Any, ...]] = []
    stdout = b'{"summary":"ok","event":"COMMENT","comments":[]}\n'
    _patch_subprocess_sequence(
        monkeypatch,
        [
            _FakeProc(1, stderr=b"Error: primary usage limit reached\n"),
            _FakeProc(1, stderr=b"Error: reserve usage limit reached\n"),
            _FakeProc(0, stdout=stdout),
        ],
        calls,
    )

    pr, dump = _sample_review_input()
    eng = CodexCliEngine(
        binary="codex",
        model="gpt-6-astra",
        fallback_models=(fallback_model, "gpt-5.3-codex-spark"),
        reasoning_effort=primary_effort,
        fallback_reasoning_effort="max",
    )

    result = await eng.review(pr, dump)

    assert result.model_used == "gpt-5.3-codex-spark"
    assert result.reasoning_effort_used == "xhigh"
    assert f"model_reasoning_effort={primary_effort}" in calls[0]
    assert fallback_model in calls[1]
    assert "model_reasoning_effort=max" in calls[1]
    assert "model_reasoning_effort=xhigh" in calls[2]


async def test_review_records_successful_reserve_model_uses_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    stdout = b'{"summary":"ok","event":"COMMENT","comments":[]}\n'
    _patch_subprocess_sequence(
        monkeypatch,
        [
            _FakeProc(1, stderr=b"Error: primary usage limit reached\n"),
            _FakeProc(0, stdout=stdout),
        ],
        calls,
    )

    pr, dump = _sample_review_input()
    eng = CodexCliEngine(
        binary="codex",
        model="gpt-6-astra",
        fallback_models=("gpt-reserve", "gpt-5.3-codex-spark"),
        reasoning_effort="max",
    )

    result = await eng.review(pr, dump)

    assert result.model_used == "gpt-reserve"
    assert result.reasoning_effort_used == "max"
    assert "model_reasoning_effort=max" in calls[0]
    assert "model_reasoning_effort=max" in calls[1]


async def test_review_records_successful_primary_model_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    stdout = b'{"summary":"ok","event":"COMMENT","comments":[]}\n'
    _patch_subprocess_sequence(
        monkeypatch,
        [_FakeProc(0, stdout=stdout, stderr=b"")],
        calls,
    )

    pr, dump = _sample_review_input()
    eng = CodexCliEngine(
        binary="codex",
        model="gpt-5.3-codex-spark",
        fallback_models=("gpt-5.5",),
        reasoning_effort="xhigh",
    )

    result = await eng.review(pr, dump)

    assert result.summary == "ok"
    assert result.model_used == "gpt-5.3-codex-spark"
    assert result.reasoning_effort_used == "xhigh"
    assert "model_reasoning_effort=xhigh" in calls[0]


async def test_review_limits_total_timeout_across_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    class _FakeLoop:
        def __init__(self) -> None:
            self._time = 0.0

        def time(self) -> float:
            return self._time

    fake_loop = _FakeLoop()

    async def _fake_review(
        _prompt: str,
        _dump: FileDump,
        *,
        model: str,
        timeout_sec: float,
    ) -> ReviewResult:
        calls.append((model, timeout_sec))
        if model == "gpt-5.3-codex-spark":
            fake_loop._time = 4.0
            raise ReviewEngineError("first model failed")
        return ReviewResult(summary="ok", event=ReviewEvent.COMMENT)

    pr, dump = _sample_review_input()
    eng = CodexCliEngine(
        binary="codex",
        model="gpt-5.3-codex-spark",
        fallback_models=("gpt-5.5",),
        timeout_sec=5,
    )
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: fake_loop)
    eng._review_with_model = _fake_review  # type: ignore[method-assign]

    result = await eng.review(pr, dump)

    assert result.summary == "ok"
    assert [model for model, _ in calls] == ["gpt-5.3-codex-spark", "gpt-5.5"]
    assert calls[0][1] == 5.0
    assert calls[1][1] == 1.0


async def test_review_raises_when_timeout_budget_is_exhausted_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    class _FakeLoop:
        def __init__(self) -> None:
            self._time = 0.0

        def time(self) -> float:
            return self._time

    fake_loop = _FakeLoop()

    async def _fake_review(
        _prompt: str,
        _dump: FileDump,
        *,
        model: str,
        timeout_sec: float,
    ) -> ReviewResult:
        calls.append((model, timeout_sec))
        fake_loop._time += 6.0
        raise ReviewEngineError(f"{model} failed")

    pr, dump = _sample_review_input()
    eng = CodexCliEngine(
        binary="codex",
        model="gpt-5.3-codex-spark",
        fallback_models=("gpt-5.5",),
        timeout_sec=5,
    )
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: fake_loop)
    eng._review_with_model = _fake_review  # type: ignore[method-assign]

    with pytest.raises(ReviewEngineError) as exc_info:
        await eng.review(pr, dump)

    assert "timeout budget exhausted" in str(exc_info.value)
    assert exc_info.value.__cause__ is not None
    assert "gpt-5.3-codex-spark failed" in str(exc_info.value.__cause__)
    assert calls == [("gpt-5.3-codex-spark", 5.0)]


async def test_review_reports_attempted_models_when_fallbacks_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    _patch_subprocess_sequence(
        monkeypatch,
        [
            _FakeProc(1, stdout=b"", stderr=b"Error: spark unavailable\n"),
            _FakeProc(1, stdout=b"", stderr=b"Error: gpt quota exhausted\n"),
        ],
        calls,
    )

    pr, dump = _sample_review_input()
    eng = CodexCliEngine(
        binary="codex",
        model="gpt-5.3-codex-spark",
        fallback_models=("gpt-5.5",),
    )

    with pytest.raises(ReviewEngineError) as exc_info:
        await eng.review(pr, dump)

    msg = str(exc_info.value)
    assert "gpt-5.3-codex-spark -> gpt-5.5" in msg
    assert "last error" in msg
    assert "gpt quota exhausted" in msg
    assert exc_info.value.returncode == 1
    assert [call[call.index("--model") + 1] for call in calls] == [
        "gpt-5.3-codex-spark",
        "gpt-5.5",
    ]


async def test_review_masks_credentials_in_review_engine_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회귀 (codex PR #18 Critical): stderr 마지막 줄에 토큰 URL 이 있으면
    `ReviewEngineError` 메시지에도 마스킹된 형태로만 들어가야 한다.
    이 메시지는 logger.exception traceback 이나 PR 진단 코멘트로 흘러가는데, 현재
    `_RedactFilter` 는 traceback 안의 exc 문자열을 재마스킹하지 않으므로 **예외 생성
    시점에 직접 마스킹** 해야 누출 표면이 막힌다.
    """
    from codex_review.domain import FileDump, FileEntry, PullRequest, RepoRef
    stderr = (
        b"OpenAI Codex v0.124.0\n"
        b"--------\n"
        b"fatal: unable to access 'https://x-access-token:ghs_LEAKED@github.com/o/r.git'\n"
    )
    _patch_subprocess(monkeypatch, _FakeProc(1, stdout=b"", stderr=stderr))

    pr = PullRequest(
        repo=RepoRef("o", "r"), number=1, title="t", body="",
        head_sha="abc", head_ref="feat", base_sha="def", base_ref="main",
        clone_url="https://example/x.git", changed_files=("a.py",),
        installation_id=7, is_draft=False,
    )
    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x", size_bytes=1, is_changed=True),),
        total_chars=1,
    )

    eng = CodexCliEngine(binary="codex", model="gpt-5.5")
    with pytest.raises(ReviewEngineError) as exc_info:
        await eng.review(pr, dump)

    msg = str(exc_info.value)
    # 토큰은 절대 메시지에 들어가면 안 된다.
    assert "ghs_LEAKED" not in msg
    # URL 자격증명은 마스킹된 형태로 표시.
    assert "https://***@github.com" in msg
    # returncode 정보는 유지 (도메인 메타데이터).
    assert exc_info.value.returncode == 1


@pytest.mark.parametrize(
    "stderr",
    [
        (
            b"ERROR: Codex ran out of room in the model's context window. "
            b"Start a new thread or clear earlier history before retrying.\n"
            b"tokens used\n"
            b"0\n"
        ),
        (
            b"ERROR: Codex ran out of room in the model's context window. "
            b"Start a new thread or clear earlier history before retrying.\n"
            b"tokens used / 0\n"
        ),
        (
            b"ERROR: Codex ran out of room in the model's context window. "
            b"Start a new thread or clear earlier history before retrying.\n"
            b"tokens used\n"
            b"1,234\n"
        ),
    ],
)
async def test_review_uses_context_error_before_tokens_used_footer(
    monkeypatch: pytest.MonkeyPatch,
    stderr: bytes,
) -> None:
    _patch_subprocess(monkeypatch, _FakeProc(1, stdout=b"", stderr=stderr))

    pr = PullRequest(
        repo=RepoRef("o", "r"), number=1, title="t", body="",
        head_sha="abc", head_ref="feat", base_sha="def", base_ref="main",
        clone_url="https://example/x.git", changed_files=("a.py",),
        installation_id=7, is_draft=False,
    )
    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x", size_bytes=1, is_changed=True),),
        total_chars=1,
    )

    eng = CodexCliEngine(binary="codex", model="gpt-5.5")
    with pytest.raises(ReviewEngineError) as exc_info:
        await eng.review(pr, dump)

    msg = str(exc_info.value)
    assert "Codex ran out of room" in msg
    assert not msg.endswith(": 0")
