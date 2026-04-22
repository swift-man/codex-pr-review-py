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

    같은 자료구조를 재사용해 하위 파이프라인(프롬프트 빌더, 토큰 예산, 라인 필터)이
    모드별로 분기만 하면 되도록 설계.
    """

    entries: tuple[FileEntry, ...]
    total_chars: int
    excluded: tuple[str, ...] = field(default_factory=tuple)
    exceeded_budget: bool = False
    budget: TokenBudget | None = None
    mode: str = DUMP_MODE_FULL
    # diff 모드에서 patch 가 누락돼 리뷰 대상에서 제외된 파일 (rename/delete/binary/거대 diff).
    # 본문 배지에 "이 파일들은 diff 를 제공받지 못해 리뷰 불가" 로 노출한다.
    patch_missing: tuple[str, ...] = field(default_factory=tuple)
