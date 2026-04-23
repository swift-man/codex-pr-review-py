"""전체 코드베이스 컨텍스트가 예산을 초과한 PR 에 대해 unified patch 만으로 리뷰
가능한 `FileDump` 를 만드는 collector.

핵심 결정:
  - GitHub 가 돌려준 patch 원문을 그대로 `FileEntry.content` 에 담는다. `@@ -a,b +c,d @@`
    hunk 헤더 + `+`/`-`/` ` 접두가 이미 LLM 에게 변경 범위를 명확히 전달한다.
  - 파일 단위 우선순위는 PR.changed_files 의 원래 순서를 유지한다. diff 를 GitHub 는
    대체로 중요도·변경량 순에 가깝게 돌려주는 경향이 있고, 임의 재정렬보다는 결정론적
    순서가 운영자가 뭐가 포함되고 뭐가 잘렸는지 추적하기 쉽다.
  - 예산 초과가 되는 순간 이후 파일을 건너뛰되 완전히 drop 한다. 부분 patch 를 잘라
    보내면 hunk 경계가 깨져 라인 번호 해석이 어긋날 수 있다 — 정확성을 희생하느니
    전체 단위로 빠지는 편이 안전.
  - **프롬프트 고정 오버헤드(system rules · PR metadata · SCOPE 섹션) 를 예약한 뒤**
    남은 공간에만 patch 를 담는다. 예산 판정을 patch 본문만으로 하면 최종
    `build_prompt()` 결과가 실제로는 `CODEX_MAX_INPUT_TOKENS` 를 넘어 `codex exec`
    단계에서 실패한다 — codex 리뷰 Major 지적 반영.
  - `patch_missing` 는 변경 파일 중 GitHub 가 patch 를 주지 않은 항목 (rename / delete /
    binary / 거대 diff). 리뷰 본문 배지로 운영자에게 노출한다.
"""

import logging
from collections.abc import Callable

from codex_review.domain import (
    DUMP_MODE_DIFF,
    FileDump,
    FileEntry,
    PullRequest,
    TokenBudget,
)

from .codex_prompt import build_prompt

logger = logging.getLogger(__name__)

OverheadEstimator = Callable[[PullRequest, FileDump], int]


def _default_overhead_estimator(pr: PullRequest, empty_dump: FileDump) -> int:
    """실 운영 기본값 — `build_prompt()` 를 1회 호출해 오버헤드를 측정."""
    return len(build_prompt(pr, empty_dump))


class DiffContextCollector:
    """`DiffContextCollector` Protocol 의 기본 구현 — 원문 patch 를 예산 안에서 축적.

    `overhead_estimator` 는 "빈 덤프 + 확정된 patch_missing" 상태의 프롬프트 크기를
    돌려주는 콜백. 기본은 실제 `build_prompt()` 를 쓰지만, 단위 테스트는 고정값/0 을
    돌려주는 stub 을 주입해 overhead 비의존 truncation 동작만 분리 검증할 수 있다.
    """

    def __init__(self, overhead_estimator: OverheadEstimator | None = None) -> None:
        self._estimate_overhead = overhead_estimator or _default_overhead_estimator

    async def collect_diff(self, pr: PullRequest, budget: TokenBudget) -> FileDump:
        max_chars = budget.max_chars()
        # 1st pass: patch 없는 파일을 먼저 분류한다. SCOPE 섹션에 들어가므로 오버헤드
        # 계산에도 정확히 반영돼야 한다.
        patch_missing = tuple(p for p in pr.changed_files if p not in pr.diff_patches)

        # 오버헤드 산정: "빈 덤프 + 이미 아는 patch_missing" 으로 프롬프트를 한 번 만들어
        # 그 길이를 측정한다. budget_trimmed 목록은 이 시점에 알 수 없지만, SCOPE 섹션에서
        # 각 항목이 차지하는 바이트는 수십 바이트 수준이라 실무적으로 무시 가능한 오차.
        overhead_estimate_dump = FileDump(
            entries=(),
            total_chars=0,
            excluded=patch_missing,
            patch_missing=patch_missing,
            mode=DUMP_MODE_DIFF,
            budget=budget,
        )
        overhead_chars = self._estimate_overhead(pr, overhead_estimate_dump)
        # 오버헤드가 예산 전체를 삼킨 경우 정직하게 0 반환. use case 가 "빈 덤프" 로
        # 판정해 fallback 불가 안내로 떨어지게 한다. 인위적 floor 로 "사실상 초과" 상태
        # 를 숨기면 codex 단계에서 더 혼란스러운 실패로 이어진다.
        patch_budget = max(0, max_chars - overhead_chars)

        entries: list[FileEntry] = []
        budget_trimmed: list[str] = []
        total_chars = 0
        budget_full = patch_budget <= 0

        for path in pr.changed_files:
            patch = pr.diff_patches.get(path)
            if patch is None:
                # 이미 1st pass 에서 patch_missing 리스트에 담긴 파일 — skip.
                continue

            if budget_full:
                # 이미 예산이 찼으므로 더 담지 않지만, 이 파일이 "리뷰되지 않았다" 는 사실은
                # budget_trimmed 에 남겨 배지·프롬프트 SCOPE 섹션에 정확히 표시되도록 한다.
                budget_trimmed.append(path)
                continue

            # `@@ -... +... @@` hunk 가 이미 파일 경로를 포함하지 않는다. LLM 이 어떤
            # 파일의 변경인지 알 수 있도록 얇은 파일 헤더를 붙여 내보낸다.
            body = f"=== PATCH: {path} ===\n{patch.rstrip()}\n"
            size_chars = len(body)
            size_bytes = len(body.encode("utf-8"))

            if total_chars + size_chars > patch_budget:
                budget_trimmed.append(path)
                budget_full = True
                logger.warning(
                    "diff collector: patch budget exceeded after %d entries (%d/%d chars, "
                    "overhead=%d) — dropping %s and subsequent changed files",
                    len(entries), total_chars, patch_budget, overhead_chars, path,
                )
                continue

            entries.append(
                FileEntry(path=path, content=body, size_bytes=size_bytes, is_changed=True)
            )
            total_chars += size_chars

        exceeded = bool(budget_trimmed)

        logger.info(
            "diff collector: files=%d total_chars=%d (patch_budget=%d, overhead=%d) "
            "budget_trimmed=%d patch_missing=%d",
            len(entries), total_chars, patch_budget, overhead_chars,
            len(budget_trimmed), len(patch_missing),
        )

        return FileDump(
            entries=tuple(entries),
            total_chars=total_chars,
            # `excluded` 는 기존 FileDump 계약상 "그 외 이유로 빠진 파일" 로 쓰이므로,
            # budget_trimmed 와 patch_missing 둘 다 합쳐 운영자 노출용으로 넣는다.
            excluded=tuple(budget_trimmed) + patch_missing,
            exceeded_budget=exceeded,
            budget=budget,
            mode=DUMP_MODE_DIFF,
            patch_missing=patch_missing,
        )
