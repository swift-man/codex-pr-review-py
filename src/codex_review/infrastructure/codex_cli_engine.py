import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace

from codex_review.domain import (
    DUMP_MODE_DIFF,
    FileDump,
    FileEntry,
    PullRequest,
    ReviewHistory,
    ReviewResult,
    TokenBudget,
)
from codex_review.interfaces import ReviewEngineError
from codex_review.logging_utils import redact_text
from codex_review.model_utils import (
    DEFAULT_CODEX_REASONING_EFFORT,
    ReasoningEffort,
    dedupe_models,
    effective_reasoning_effort,
)

from ._subprocess import kill_and_reap
from .codex_parser import parse_review
from .codex_prompt import build_prompt
from .file_dump_collector import estimate_full_prompt_file_chars

logger = logging.getLogger(__name__)

# Codex CLI 0.144.1의 turn/start 입력 하드 제한. 리뷰 이력과 prompt 헤더가 수집
# 결과 뒤에 추가되므로 collector에는 여유를 둔 별도 상한을 주입한다.
CODEX_CLI_MAX_INPUT_CHARS = 1_048_576
CODEX_CLI_COLLECTOR_MAX_CHARS = 1_000_000

_STDERR_TOKENS_USED_MARKER = "tokens used"
_STDERR_EMPTY_SUMMARY = "(no stderr)"
_STDERR_TOKEN_COUNT_SEPARATORS = ("/", ":")


class CodexAuthError(RuntimeError):
    """Raised when the Codex CLI is not authenticated (manual `codex login` required)."""


class CodexCliEngine:
    """Async wrapper around `codex exec`. stdin 으로 프롬프트를 넘기고 stdout JSON 을 파싱."""

    def __init__(
        self,
        binary: str = "codex",
        model: str = "gpt-5.6-sol",
        fallback_models: Sequence[str] = (),
        reasoning_effort: ReasoningEffort = DEFAULT_CODEX_REASONING_EFFORT,
        primary_context_window: int | None = None,
        timeout_sec: int = 600,
        fallback_reasoning_effort: ReasoningEffort | None = None,
        model_input_budgets: Mapping[str, int] | None = None,
    ) -> None:
        if primary_context_window is not None and primary_context_window <= 0:
            raise ValueError("primary_context_window must be positive")
        self._binary = binary
        self._models = dedupe_models((model, *fallback_models))
        self._reasoning_effort = reasoning_effort
        self._fallback_reasoning_effort = fallback_reasoning_effort or reasoning_effort
        self._primary_context_window = primary_context_window
        self._model_input_budgets = dict(model_input_budgets or {})
        self._timeout_sec = timeout_sec

    async def verify_auth(self) -> str:
        """Run `codex login status` and return the status line, or raise CodexAuthError.

        기동 시 호출해 토큰이 살아 있는지 선점검한다. 실패하면 서버 기동 자체를 막아
        운영자가 `codex login` 을 먼저 실행하도록 유도.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary, "login", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise CodexAuthError(
                f"CODEX_BIN='{self._binary}' 을(를) 실행할 수 없습니다. "
                "경로를 확인하거나 `codex` CLI를 설치하세요."
            ) from exc

        try:
            async with asyncio.timeout(10.0):
                stdout, stderr = await proc.communicate()
        except TimeoutError as exc:
            # kill 후 wait 자체에도 상한을 둔다 — 수거가 지연돼도 서버 기동 경로가 붙잡히지 않도록.
            await kill_and_reap(proc, process_group=True)
            raise CodexAuthError("codex login status 가 10초 내에 응답하지 않았습니다.") from exc
        except asyncio.CancelledError:
            # 워커 취소/서버 종료 신호 시 하위 프로세스가 좀비로 남지 않도록 반드시 정리.
            await kill_and_reap(proc, process_group=True)
            raise
        except BaseException:
            # 예상하지 못한 예외도 자식 프로세스 누수로 이어지지 않도록 정리 후 재전파.
            await kill_and_reap(proc, process_group=True)
            raise

        # 래퍼가 어떤 코드로 끝났든 세션에 남은 네이티브 자식은 전부 고아다. 성공 분기만
        # 빼놓으면 정상 종료가 대부분인 실제 운영에서 오히려 더 많이 샌다.
        await kill_and_reap(proc, process_group=True)

        # codex CLI 는 TTY 가 아닐 때 상태 메시지를 stderr 로 보내므로 두 스트림 모두 확인.
        combined = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()
        if proc.returncode != 0 or "Logged in" not in combined:
            raise CodexAuthError(
                "Codex CLI 가 로그인되어 있지 않습니다.\n"
                f"출력: {combined or '(empty)'}\n"
                f"해결: 터미널에서 `{self._binary} login` 을 실행해 ChatGPT 로 로그인한 뒤 "
                "서버를 재기동하세요."
            )
        return combined.splitlines()[0] if combined else "Logged in"

    async def review(
        self,
        pr: PullRequest,
        dump: FileDump,
        *,
        history: ReviewHistory | None = None,
    ) -> ReviewResult:
        last_error: ReviewEngineError | None = None
        # 모델별 실패 사유를 전부 보관한다. 이전에는 마지막 모델의 오류만 예외 메시지에
        # 실려, 체인 끝에 영구 사용 불가 모델이 있으면 그 오류가 앞 모델의 진짜 원인(한도
        # 초과 등)을 가렸다. 운영자는 PR 진단 코멘트로만 원인을 보므로 치명적이다.
        failures: list[tuple[str, ReviewEngineError]] = []
        attempted_models: list[str] = []
        deadline = asyncio.get_running_loop().time() + self._timeout_sec
        for idx, model in enumerate(self._models):
            remaining_sec = deadline - asyncio.get_running_loop().time()
            if remaining_sec <= 0:
                break
            attempted_models.append(model)
            try:
                attempt_dump = self._dump_for_model(
                    pr,
                    dump,
                    model,
                    history=history,
                    # 뒤에 시도할 모델이 남아 있을 때만 예산 초과를 이유로 이 모델을
                    # 건너뛴다. 마지막 모델까지 건너뛰면 리뷰 경로가 통째로 사라진다.
                    allow_skip=idx + 1 < len(self._models),
                )
                prompt = build_prompt(pr, attempt_dump, history=history)
                if len(prompt) > CODEX_CLI_MAX_INPUT_CHARS:
                    raise ReviewEngineError(
                        "codex turn/start input is too long "
                        f"(max_chars={CODEX_CLI_MAX_INPUT_CHARS}, actual_chars={len(prompt)})"
                    )
                return await self._review_with_model(
                    prompt,
                    attempt_dump,
                    model=model,
                    timeout_sec=remaining_sec,
                )
            except ReviewEngineError as exc:
                last_error = exc
                failures.append((model, exc))
                next_model = self._models[idx + 1] if idx + 1 < len(self._models) else None
                if next_model is None:
                    break
                logger.warning(
                    "codex model failed; trying fallback model "
                    "(failed_model=%s next_model=%s): %s",
                    model,
                    next_model,
                    exc,
                )

        if last_error is None:
            raise ReviewEngineError(
                "codex exec timeout budget exhausted before any model was attempted"
            )
        # 여기 도달했다면 루프 안에서 최소 한 번은 시도한 것이다 (`last_error` 가 그
        # 안에서만 설정된다). 따라서 `attempted_models` 는 절대 비지 않는다.
        attempted = " -> ".join(attempted_models)
        model_failures = tuple((model, str(error)) for model, error in failures)
        if asyncio.get_running_loop().time() >= deadline:
            budget_error = ReviewEngineError(
                f"codex exec timeout budget exhausted (attempted={attempted}); "
                f"{_format_model_failures(failures)}",
                model_failures=model_failures,
            )
            raise budget_error from last_error
        if len(self._models) == 1:
            # 메시지는 그대로 두되 구조화 목록은 채운다 — 단일 모델 구성에서도 상위
            # 계층이 "이 모델은 못 쓴다" 진단을 낼 수 있어야 한다 (gemini PR #58 Major).
            last_error.model_failures = model_failures
            raise last_error
        raise ReviewEngineError(
            f"codex exec fallback exhausted (models={attempted}); "
            f"{_format_model_failures(failures)}",
            returncode=last_error.returncode,
            model_failures=model_failures,
        ) from last_error

    def _dump_for_model(
        self,
        pr: PullRequest,
        dump: FileDump,
        model: str,
        *,
        history: ReviewHistory | None,
        allow_skip: bool = True,
    ) -> FileDump:
        """Trim the in-memory snapshot to the selected model's input budget.

        The collector runs once under the repository snapshot lock. Fallback attempts therefore
        trim entries from that immutable snapshot instead of rereading a moving checkout.

        `allow_skip` 는 "예산에 못 맞추면 이 모델을 건너뛰어도 되는가" — 뒤에 시도할 모델이
        남아 있을 때만 True 다. 마지막 모델에서는 예산을 못 맞춰도 최대한 줄인 입력으로
        실제 호출을 시도한다. 우리 예산(4 chars/token) 은 보수적 추정일 뿐이라, 여기서
        포기하면 CLI 가 받아들였을 입력까지 버리고 리뷰를 통째로 잃는다.
        """
        configured = self._model_input_budgets.get(model)
        if configured is None:
            return dump
        max_chars = min(configured * 4, CODEX_CLI_MAX_INPUT_CHARS)
        entries = list(dump.entries)
        removable = [entry for entry in entries if not entry.is_changed]

        def candidate_for(remove_count: int) -> FileDump:
            removed_entries = removable[-remove_count:] if remove_count else []
            removed_paths = {entry.path for entry in removed_entries}
            kept_entries = tuple(entry for entry in entries if entry.path not in removed_paths)
            excluded = list(dump.excluded)
            known = set(excluded)
            for entry in removed_entries:
                if entry.path not in known:
                    excluded.append(entry.path)
                    known.add(entry.path)
            return replace(
                dump,
                entries=kept_entries,
                total_chars=_dump_total_chars(dump.mode, kept_entries),
                excluded=tuple(excluded),
                exceeded_budget=dump.exceeded_budget or bool(removed_entries),
                # 수집 단계 예산이 아니라 이 시도에 실제로 적용한 모델 예산을 싣는다.
                # `max_chars()` 가 위 `max_chars` 와 같은 값을 내도록 상한도 함께 준다.
                budget=TokenBudget(
                    max_tokens=configured, max_chars_limit=CODEX_CLI_MAX_INPUT_CHARS
                ),
            )

        if len(build_prompt(pr, dump, history=history)) <= max_chars:
            return dump

        # Changed files are the review target and must never be silently discarded. In diff
        # mode every entry is a changed file, so `changed_only` is the original dump — there is
        # nothing this method can trim and the budget can only be enforced by skipping.
        changed_only = candidate_for(len(removable))
        changed_only_chars = len(build_prompt(pr, changed_only, history=history))
        if changed_only_chars > max_chars:
            if allow_skip:
                raise ReviewEngineError(
                    f"codex input exceeds the model={model} token limit "
                    f"(max_chars={max_chars}, actual_chars={changed_only_chars})"
                )
            logger.warning(
                "codex input still over budget on last model=%s after dropping %d unchanged "
                "files (max_chars=%d, actual_chars=%d) — attempting anyway",
                model, len(removable), max_chars, changed_only_chars,
            )
            return changed_only

        # Removing one file at a time rebuilds the full prompt O(N^2) times. Find the smallest
        # suffix of low-priority, unchanged files that makes the prompt fit instead.
        low, high = 0, len(removable)
        while low < high:
            middle = (low + high) // 2
            candidate = candidate_for(middle)
            if len(build_prompt(pr, candidate, history=history)) <= max_chars:
                high = middle
            else:
                low = middle + 1
        trimmed = candidate_for(low)
        if low:
            logger.warning(
                "trimmed review snapshot for model=%s: dropped %d unchanged files "
                "(files %d -> %d, max_chars=%d)",
                model, low, len(entries), len(trimmed.entries), max_chars,
            )
        return trimmed

    async def _review_with_model(
        self,
        prompt: str,
        dump: FileDump,
        *,
        model: str,
        timeout_sec: float,
    ) -> ReviewResult:
        requested_effort = (
            self._reasoning_effort
            if model == self._models[0]
            else self._fallback_reasoning_effort
        )
        model_reasoning_effort = effective_reasoning_effort(model, requested_effort)
        # "-" positional 은 codex exec 에 stdin 에서 프롬프트를 읽으라는 지시.
        # argv 로 넘기면 전체 레포 덤프가 ARG_MAX 를 초과할 수 있어 stdin 이 안전.
        logger.info(
            "invoking codex: files=%d chars=%d model=%s effort=%s context_window=%s",
            len(dump.entries),
            dump.total_chars,
            model,
            model_reasoning_effort,
            self._context_window_for(model) or "default",
        )
        command = [
            self._binary,
            "exec",
            "--model",
            model,
            # reasoning_effort 는 config 오버라이드로 넘긴다 — `codex exec` 가 별도 CLI
            # 플래그로 지원하지 않고 ~/.codex/config.toml 값만 읽기 때문.
            "--config",
            f"model_reasoning_effort={model_reasoning_effort}",
        ]
        if (context_window := self._context_window_for(model)) is not None:
            command.extend(("--config", f"model_context_window={context_window}"))
        command.append("-")
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        try:
            async with asyncio.timeout(timeout_sec):
                stdout, stderr = await proc.communicate(input=prompt.encode("utf-8"))
        except TimeoutError as exc:
            # 하위 프로세스 수거 대기에도 상한 — 큐 동시성 상한이 `CODEX_TIMEOUT_SEC` 을
            # 훨씬 넘겨 점유되는 걸 막는다 (codex 리뷰 지적).
            await kill_and_reap(proc, process_group=True)
            # 타임아웃은 "엔진이 입력 처리에 실패" 의 한 형태이므로 ReviewEngineError 로
            # 분류 — use case 가 diff fallback 으로 재시도할 수 있다 (작은 입력으로 줄이면
            # 시간 안에 끝날 수 있음).
            raise ReviewEngineError(
                f"codex exec timed out after {timeout_sec:.1f}s on model={model}"
            ) from exc
        except asyncio.CancelledError:
            # 서버 종료/워커 취소 시 `codex exec` 하위 프로세스가 좀비로 남아 토큰·쿼터·CPU 를
            # 계속 소모하지 않도록 확실히 kill + wait 후 취소를 재전파한다.
            await kill_and_reap(proc, process_group=True)
            raise
        except BaseException:
            # 인코딩/communicate 중 예상하지 못한 예외에서도 고아 프로세스가 남지 않게 정리.
            await kill_and_reap(proc, process_group=True)
            raise

        # 성공 종료도 마찬가지다. `communicate()` 가 돌아온 시점에 래퍼는 이미 끝났고,
        # 세션에 남아 있는 프로세스는 모델 연결을 붙잡은 고아뿐이다.
        await kill_and_reap(proc, process_group=True)

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            # 전체 stderr 는 별도 ERROR 로그로 — multi-line 그대로 보존되어 운영 진단이
            # 즉시 가능하다. 이전엔 stderr 가 RuntimeError 메시지에 들어가 traceback
            # summary 가 첫 줄(=Codex 시작 배너) 만 보여 진단에 시간이 걸렸다.
            # `_RedactFilter` 가 record.args 까지 마스킹하므로 토큰/URL 자격증명이 stderr
            # 에 섞여 있어도 안전 (logging_utils 갱신 — codex PR #18 Major 반영).
            logger.error(
                "codex exec failed (rc=%d, model=%s):\n%s",
                proc.returncode, model, err or "(no stderr)",
            )
            # ReviewEngineError 로 분리 — use case 가 일반 버그(KeyError 등) 와 구분해
            # diff fallback 결정을 정확히 내릴 수 있게 (gemini PR #18 Major+Suggestion 반영).
            # 메시지엔 stderr 의 **마지막 줄** 만 포함 — 보통 Codex CLI 가 마지막 줄에
            # 실제 원인(model not available, context length exceeded 등) 을 찍는다.
            #
            # 보안: 예외 메시지는 이후 `logger.exception` 의 traceback 이나 PR 진단
            # 코멘트 본문으로도 흘러간다. `_RedactFilter` 는 traceback 안의 exc 문자열은
            # 마스킹하지 않으므로, **예외에 넣기 전 단계에서 직접 마스킹** 해야 토큰 URL /
            # `authorization=Bearer ...` 같은 자격증명이 어떤 경로로도 새지 않는다
            # (codex PR #18 Critical 반영).
            summary = _summarize_stderr(err)
            raise ReviewEngineError(
                f"codex exec failed (rc={proc.returncode}, model={model}): {summary}",
                returncode=proc.returncode,
            )

        return replace(
            parse_review(stdout.decode(errors="replace")),
            model_used=model,
            reasoning_effort_used=model_reasoning_effort,
        )

    def _context_window_for(self, model: str) -> int | None:
        """Apply the expanded context only to the primary model.

        Fallback models have smaller catalog windows. Passing the primary override to them would
        hide that constraint from Codex CLI and defer failure to the API.
        """
        if model != self._models[0]:
            return None
        return self._primary_context_window


def _summarize_stderr(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in reversed(lines):
        if _is_codex_stderr_footer_line(line):
            continue
        return redact_text(line)
    return _STDERR_EMPTY_SUMMARY


def _is_codex_stderr_footer_line(line: str) -> bool:
    lowered = line.lower()
    if lowered == _STDERR_TOKENS_USED_MARKER or lowered.replace(",", "").isdecimal():
        return True
    if not lowered.startswith(_STDERR_TOKENS_USED_MARKER):
        return False

    suffix = lowered.removeprefix(_STDERR_TOKENS_USED_MARKER).strip()
    if not suffix or suffix[0] not in _STDERR_TOKEN_COUNT_SEPARATORS:
        return False
    token_count = suffix[1:].strip().replace(",", "")
    return token_count.isdecimal()


def _dump_total_chars(mode: str, entries: Sequence[FileEntry]) -> int:
    """Recompute `FileDump.total_chars` with the same unit the producing collector used.

    full 모드는 `FileDumpCollector` 와 동일한 프롬프트 문자 추정치, diff 모드는
    `DiffContextCollector` 와 동일한 UTF-8 바이트 합계다. 여기서 단위가 어긋나면
    "invoking codex: chars=..." 로그가 축소 여부에 따라 조용히 다른 값을 가리킨다.
    """
    if mode == DUMP_MODE_DIFF:
        return sum(entry.size_bytes for entry in entries)
    return sum(
        estimate_full_prompt_file_chars(
            entry.path, entry.content, is_changed=entry.is_changed
        )
        for entry in entries
    )


# 모델별 사유를 한 줄씩. 한 줄이 통째로 진단 코멘트를 잡아먹지 않도록 상한을 둔다.
_MODEL_FAILURE_LINE_MAX_CHARS = 300


def _format_model_failures(failures: Sequence[tuple[str, ReviewEngineError]]) -> str:
    """체인의 모든 모델 실패를 사람이 읽을 `[모델] 사유` 줄로 나열한다.

    마지막 오류만 남기면 체인 끝의 영구 실패(미지원 모델 등)가 앞 모델의 실제 원인을
    덮어 버린다. 이 문자열은 **표시 전용** 이다 — 상위 계층은 되파싱하지 말고
    `ReviewEngineError.model_failures` 를 읽어야 한다.
    """
    lines = []
    for model, error in failures:
        detail = str(error)
        if len(detail) > _MODEL_FAILURE_LINE_MAX_CHARS:
            detail = detail[:_MODEL_FAILURE_LINE_MAX_CHARS] + "…"
        lines.append(f"[{model}] {detail}")
    return "errors: " + " | ".join(lines)
