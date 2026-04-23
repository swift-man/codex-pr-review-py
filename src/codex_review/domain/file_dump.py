from dataclasses import dataclass, field


# `FileDump.mode` 가 가질 수 있는 값. 공개 상수로 고정해 infra/application 계층이
# 리터럴에 의존하지 않도록 한다.
DUMP_MODE_FULL = "full"      # 전체 코드베이스 덤프 (기본)
DUMP_MODE_DIFF = "diff"      # 컨텍스트 예산 초과 시 자동 fallback — PR unified patch 만


@dataclass(frozen=True)
class TokenBudget:
    max_tokens: int

    @staticmethod
    def chars_per_token() -> int:
        return 4

    def fits(self, char_count: int) -> bool:
        return char_count <= self.max_tokens * self.chars_per_token()

    def max_chars(self) -> int:
        return self.max_tokens * self.chars_per_token()


@dataclass(frozen=True)
class FileEntry:
    path: str
    content: str
    size_bytes: int
    is_changed: bool


@dataclass(frozen=True)
class FileDump:
    """LLM 에 넘길 파일 스냅샷. 모드에 따라 `entries[].content` 의 의미가 달라진다.

    - `mode == "full"` — 파일 전체 내용 (1-based 줄 번호 접두로 프롬프트에 표시)
    - `mode == "diff"` — GitHub unified patch 원문 (`@@ -a,b +c,d @@` 헤더 포함)

    제외 분류:
      - `filter_excluded` — 바이너리/미디어/크기 한도 등 **정책상** 뺀 파일.
        예산과 무관하게 해당 PR 이 그 파일만 바꿨어도 리뷰는 불가하므로
        fallback 을 트리거하면 안 된다.
      - `patch_missing` (diff 모드) — GitHub 가 patch 를 안 준 파일 (rename/delete/binary/거대 diff).
      - `budget_trimmed` (property) — 순수하게 **예산 때문에** 잘린 파일 (`excluded -
        filter_excluded - patch_missing`). 이것만이 "리뷰가 얕아진" 실제 원인.

    운영 관측용 `excluded` 는 세 카테고리의 합집합을 유지한다 (역호환).
    """

    entries: tuple[FileEntry, ...]
    total_chars: int
    excluded: tuple[str, ...] = field(default_factory=tuple)
    exceeded_budget: bool = False
    budget: TokenBudget | None = None
    mode: str = DUMP_MODE_FULL
    # 정책(바이너리/크기/화이트리스트) 로 배제된 파일. full collector 가 채운다.
    # diff 모드에서는 항상 비어 있다 (정책 필터는 diff 모드에 없다).
    filter_excluded: tuple[str, ...] = field(default_factory=tuple)
    # diff 모드에서 patch 가 누락돼 리뷰 대상에서 제외된 파일 (rename/delete/binary/거대 diff).
    # 본문 배지에 "이 파일들은 diff 를 제공받지 못해 리뷰 불가" 로 노출한다.
    patch_missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def budget_trimmed(self) -> tuple[str, ...]:
        """`excluded` 중 **예산 초과로만** 잘린 파일 목록.

        `excluded = budget_trimmed ∪ filter_excluded ∪ patch_missing` 이므로
        나머지 두 카테고리를 빼면 순수 예산 컷이 남는다. 이 구분이 중요한 이유:
        fallback 트리거는 "예산 때문에 변경 파일이 빠졌을 때" 만 의미가 있고,
        바이너리 파일이 정책으로 빠진 PR 까지 diff 모드로 강등하면 리뷰 품질이
        불필요하게 떨어진다 (gemini 리뷰 Major 지적).
        """
        policy_set = set(self.filter_excluded) | set(self.patch_missing)
        return tuple(p for p in self.excluded if p not in policy_set)
