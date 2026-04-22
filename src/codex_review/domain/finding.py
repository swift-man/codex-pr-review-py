from dataclasses import dataclass
from enum import Enum


class ReviewEvent(str, Enum):
    COMMENT = "COMMENT"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    APPROVE = "APPROVE"


# 라인 고정 기술 단위 코멘트의 4단계 등급.
#
#   Critical   장애 가능성·데이터 손실·보안 취약점·크래시 — "반드시 막아야 할 문제"
#   Major      버그 가능성·예외 처리 누락·상태 불일치·동시성 문제·큰 테스트 누락 — "머지 전에 고치는 게 좋은 문제"
#   Minor      가독성·중복 코드·네이밍·구조 개선 — "당장 큰 문제는 아니지만 개선 가치 있음"
#   Suggestion 대안 제안·취향 차이·리팩터링 아이디어 — "선택 제안"
#
# 값은 소문자 문자열로 JSON 스키마와 1:1 매핑. 화면 표기는 `SEVERITY_LABELS` 를 쓴다.
SEVERITY_CRITICAL = "critical"
SEVERITY_MAJOR = "major"
SEVERITY_MINOR = "minor"
SEVERITY_SUGGESTION = "suggestion"

# JSON 값 → PR 코멘트에 접두로 붙는 라벨. "[Critical] 내용" 형태로 일관되게 표기.
SEVERITY_LABELS: dict[str, str] = {
    SEVERITY_CRITICAL: "Critical",
    SEVERITY_MAJOR: "Major",
    SEVERITY_MINOR: "Minor",
    SEVERITY_SUGGESTION: "Suggestion",
}

# 머지를 막아야 한다고 보는 등급 집합. `event` 결정·본문 강조 등에 쓴다.
BLOCKING_SEVERITIES = frozenset({SEVERITY_CRITICAL, SEVERITY_MAJOR})

# 공개 계약 — 인프라 계층(파서)이 "허용된 등급인지" 를 검사할 때 참조한다.
# 이전 `_VALID_SEVERITIES` 는 언더스코어 prefix 로 '내부' 처럼 보였지만 실제로는
# 레이어 간 import 됐다. 이름을 public 으로 바꾸고 frozenset 으로 잠가 오·수정을 막는다.
VALID_SEVERITIES = frozenset(SEVERITY_LABELS)


@dataclass(frozen=True)
class Finding:
    """A line-anchored technical comment in Korean.

    `line` 은 필수 — RIGHT-side 에 실제 존재해야 GitHub 이 인라인으로 수락한다.
    `severity` 는 네 단계 중 하나. 알 수 없는 값이면 가장 약한 `suggestion` 으로 강등
    시켜 파이프라인이 깨지지 않게 한다.
    """

    path: str
    line: int
    body: str
    severity: str = SEVERITY_SUGGESTION

    def __post_init__(self) -> None:
        # 파서가 잘못된 값을 흘려도 안전하도록 항상 네 등급 중 하나로 수렴.
        if self.severity not in VALID_SEVERITIES:
            object.__setattr__(self, "severity", SEVERITY_SUGGESTION)

    @property
    def label(self) -> str:
        """`[Critical]` 같은 접두에 사용할 사람용 라벨."""
        return SEVERITY_LABELS[self.severity]

    @property
    def is_blocking(self) -> bool:
        """Critical 또는 Major — 머지 전에 반드시 해소돼야 하는 등급."""
        return self.severity in BLOCKING_SEVERITIES
