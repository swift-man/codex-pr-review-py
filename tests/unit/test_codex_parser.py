from codex_review.domain import ReviewEvent
from codex_review.domain.finding import (
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    SEVERITY_SUGGESTION,
)
from codex_review.infrastructure.codex_parser import parse_review


def test_parse_strict_json_with_all_sections() -> None:
    raw = """
    {
      "summary": "전반적으로 구조가 깔끔합니다.",
      "event": "REQUEST_CHANGES",
      "positives": ["Protocol을 통한 DIP 적용"],
      "must_fix": ["인증 토큰 캐시 경쟁 조건"],
      "improvements": ["도메인 계층과 인프라 계층의 경계를 더 명확히"],
      "comments": [
        {"path": "src/a.py", "line": 12, "severity": "critical", "body": "None 체크가 필요합니다."},
        {"path": "src/a.py", "line": 30, "severity": "suggestion", "body": "pathlib.Path 사용을 고려하세요."}
      ]
    }
    """
    result = parse_review(raw)
    assert result.summary.startswith("전반적으로")
    assert result.event == ReviewEvent.REQUEST_CHANGES
    assert result.positives == ("Protocol을 통한 DIP 적용",)
    assert result.must_fix == ("인증 토큰 캐시 경쟁 조건",)
    assert result.improvements == ("도메인 계층과 인프라 계층의 경계를 더 명확히",)
    assert len(result.findings) == 2
    assert result.findings[0].severity == SEVERITY_CRITICAL
    assert result.findings[0].is_blocking is True
    assert result.findings[1].severity == SEVERITY_SUGGESTION
    assert result.findings[1].is_blocking is False


def test_parse_accepts_all_four_severities() -> None:
    raw = """
    {
      "summary": "ok",
      "event": "REQUEST_CHANGES",
      "comments": [
        {"path": "a.py", "line": 1, "severity": "critical", "body": "c"},
        {"path": "a.py", "line": 2, "severity": "major", "body": "m"},
        {"path": "a.py", "line": 3, "severity": "minor", "body": "n"},
        {"path": "a.py", "line": 4, "severity": "suggestion", "body": "s"}
      ]
    }
    """
    result = parse_review(raw)
    severities = [f.severity for f in result.findings]
    assert severities == [
        SEVERITY_CRITICAL, SEVERITY_MAJOR, SEVERITY_MINOR, SEVERITY_SUGGESTION
    ]
    # is_blocking 은 Critical/Major 둘 뿐.
    assert [f.is_blocking for f in result.findings] == [True, True, False, False]


def test_parse_missing_severity_defaults_to_suggestion() -> None:
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "src/a.py", "line": 5, "body": "no severity field"}
      ]
    }
    """
    result = parse_review(raw)
    assert result.findings[0].severity == SEVERITY_SUGGESTION


def test_parse_unknown_severity_falls_back_to_suggestion() -> None:
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "src/a.py", "line": 5, "severity": "apocalyptic", "body": "x"}
      ]
    }
    """
    result = parse_review(raw)
    assert result.findings[0].severity == SEVERITY_SUGGESTION


def test_parse_legacy_must_fix_alias_normalizes_to_critical() -> None:
    """전환기 호환: 이전 프롬프트의 `must_fix`/`suggest`/`nit` 도 새 등급으로 흡수한다."""
    raw = """
    {
      "summary": "ok",
      "event": "REQUEST_CHANGES",
      "comments": [
        {"path": "a.py", "line": 1, "severity": "Must-Fix", "body": "x"},
        {"path": "a.py", "line": 2, "severity": "suggest", "body": "y"},
        {"path": "a.py", "line": 3, "severity": "nit", "body": "z"}
      ]
    }
    """
    result = parse_review(raw)
    assert [f.severity for f in result.findings] == [
        SEVERITY_CRITICAL, SEVERITY_SUGGESTION, SEVERITY_MINOR
    ]


def test_parse_missing_must_fix_field_defaults_to_empty() -> None:
    raw = """
    {"summary": "ok", "event": "COMMENT"}
    """
    result = parse_review(raw)
    assert result.must_fix == ()


def test_parse_picks_last_valid_json_when_reasoning_precedes() -> None:
    raw = (
        "사고 과정: 먼저 파일을 확인...\n"
        '{"note": "intermediate"}\n'
        'Final:\n'
        '{"summary": "최종 리뷰", "event": "REQUEST_CHANGES", "comments": []}'
    )
    result = parse_review(raw)
    assert result.summary == "최종 리뷰"
    assert result.event == ReviewEvent.REQUEST_CHANGES


def test_parse_fallbacks_to_plain_text_when_no_json() -> None:
    result = parse_review("그냥 평문 응답입니다.")
    assert "평문" in result.summary
    assert result.event == ReviewEvent.COMMENT


def test_parse_drops_findings_without_valid_line() -> None:
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "", "line": 1, "body": "empty path"},
        {"path": "src/a.py", "line": "bad", "body": "invalid line"},
        {"path": "src/b.py", "body": "no line — dropped"},
        {"path": "src/c.py", "line": 0, "body": "zero line — dropped"},
        {"path": "src/d.py", "line": 5, "body": "valid"}
      ]
    }
    """
    result = parse_review(raw)
    paths = [f.path for f in result.findings]
    assert paths == ["src/d.py"]
    assert result.findings[0].line == 5
