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
  - `patch_missing` 는 변경 파일 중 GitHub 가 patch 를 주지 않은 항목 (rename / delete /
    binary / 거대 diff). 리뷰 본문 배지로 운영자에게 노출한다.
"""

import logging

from codex_review.domain import (
    DUMP_MODE_DIFF,
    FileDump,
    FileEntry,
    PullRequest,
    TokenBudget,
)

logger = logging.getLogger(__name__)


class DiffContextCollector:
    """`DiffContextCollector` Protocol 의 기본 구현 — 원문 patch 를 예산 안에서 축적."""

    async def collect_diff(self, pr: PullRequest, budget: TokenBudget) -> FileDump:
        max_chars = budget.max_chars()
        entries: list[FileEntry] = []
        budget_trimmed: list[str] = []
        patch_missing: list[str] = []
        total_chars = 0

        for path in pr.changed_files:
            patch = pr.diff_patches.get(path)
            if patch is None:
                # GitHub 가 patch 를 생략한 파일 — diff 모드에서는 통째로 리뷰 불가.
                patch_missing.append(path)
                continue

            # `@@ -... +... @@` hunk 가 이미 파일 경로를 포함하지 않는다. LLM 이 어떤
            # 파일의 변경인지 알 수 있도록 얇은 파일 헤더를 붙여 내보낸다.
            body = f"=== PATCH: {path} ===\n{patch.rstrip()}\n"
            size = len(body)

            if total_chars + size > max_chars:
                # 이 파일 포함 시 예산 초과 — 이 파일부터 뒤는 전부 drop.
                budget_trimmed.append(path)
                logger.warning(
                    "diff collector: budget exceeded after %d entries (%d chars) "
                    "— dropping %s and beyond",
                    len(entries), total_chars, path,
                )
                break

            entries.append(
                FileEntry(path=path, content=body, size_bytes=size, is_changed=True)
            )
            total_chars += size

        # 예산 초과 플래그: 변경 파일 중 하나라도 예산 때문에 잘렸으면 True.
        # patch_missing 은 "애초에 patch 가 없어서 제외" 라 예산 이슈와 구분해 별도 보고.
        exceeded = bool(budget_trimmed)

        logger.info(
            "diff collector: files=%d total_chars=%d budget_trimmed=%d patch_missing=%d",
            len(entries), total_chars, len(budget_trimmed), len(patch_missing),
        )

        return FileDump(
            entries=tuple(entries),
            total_chars=total_chars,
            # `excluded` 는 기존 FileDump 계약상 "그 외 이유로 빠진 파일" 로 쓰이므로,
            # budget_trimmed 와 patch_missing 둘 다 합쳐 운영자 노출용으로 넣는다.
            excluded=tuple(budget_trimmed + patch_missing),
            exceeded_budget=exceeded,
            budget=budget,
            mode=DUMP_MODE_DIFF,
            patch_missing=tuple(patch_missing),
        )
