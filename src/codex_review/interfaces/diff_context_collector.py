from typing import Protocol

from codex_review.domain import FileDump, PullRequest, TokenBudget


class DiffContextCollector(Protocol):
    """전체 코드베이스 컨텍스트가 예산을 넘었을 때 fallback 으로 쓰는 diff 수집기.

    `FileCollector` 와 다른 경계인 이유:
      - 입력이 다르다 (파일시스템이 아니라 PR.diff_patches).
      - 출력 `FileDump.mode` 는 `DUMP_MODE_DIFF` 로 태깅돼, 이하 파이프라인이 "raw patch
        를 받은 상태" 로 분기 판단을 내린다.
    """

    async def collect_diff(self, pr: PullRequest, budget: TokenBudget) -> FileDump: ...
