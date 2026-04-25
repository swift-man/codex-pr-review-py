"""Regression coverage for `_RedactFilter` — secret masking in log records.

회귀 (codex PR #18 Major): 이전 구현은 `record.msg` 만 마스킹하고 `record.args` 는
무시했다. `logger.error("rc=%d, model=%s:\\n%s", rc, model, stderr)` 같은 호출에서
stderr 가 args 로 들어가는데, 거기 토큰/URL 자격증명이 섞이면 그대로 노출됐음.
"""

import logging

from codex_review.logging_utils import _RedactFilter


def _make_record(msg: str, args: object = None) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=logging.ERROR, pathname=__file__, lineno=0,
        msg=msg, args=args, exc_info=None,
    )


def _format(record: logging.LogRecord) -> str:
    """필터 적용 후 최종 포맷 결과 — 실제 포매터가 만드는 출력과 동일."""
    return logging.Formatter("%(message)s").format(record)


def test_redacts_secret_in_msg() -> None:
    """기본 동작 (회귀 방지): `msg` 안의 평문 시크릿 패턴 마스킹."""
    record = _make_record("token=abc123 something")
    _RedactFilter().filter(record)
    assert "abc123" not in record.msg
    assert "token=***" in record.msg


def test_redacts_secret_in_tuple_args() -> None:
    """회귀 (codex PR #18 Major): args 가 tuple 일 때 각 원소 안의 시크릿도 마스킹."""
    record = _make_record(
        "rc=%d, stderr=\n%s",
        (1, "Error: authorization=Bearer ghs_abc123 invalid"),
    )
    _RedactFilter().filter(record)
    formatted = _format(record)
    assert "ghs_abc123" not in formatted
    assert "authorization=***" in formatted


def test_redacts_url_userinfo_in_args() -> None:
    """codex stderr 에 git/GitHub URL 자격증명이 섞이면 마스킹."""
    record = _make_record(
        "stderr=\n%s",
        ("fatal: unable to access 'https://x-access-token:ghs_xxx@github.com/o/r.git'",),
    )
    _RedactFilter().filter(record)
    formatted = _format(record)
    assert "ghs_xxx" not in formatted
    assert "https://***@github.com" in formatted


def test_redacts_secret_in_dict_args() -> None:
    """`%(key)s` 포맷의 dict args 도 값 마스킹.

    logging 컨벤션: `logger.info("x %(k)s", {"k": "v"})` 호출 시 LogRecord 내부에선
    args 가 `({"k": "v"},)` 로 1-tuple 래핑된다. 필터가 이 형태를 인식해야 한다.
    """
    record = _make_record(
        "leak: %(detail)s",
        ({"detail": "secret=topsecret123 leaked"},),
    )
    _RedactFilter().filter(record)
    formatted = _format(record)
    assert "topsecret123" not in formatted
    assert "secret=***" in formatted


def test_redacts_secret_in_single_value_arg() -> None:
    """드물지만 args 가 단일 값(non-tuple) 인 경우도 처리."""
    # logging 모듈은 1개 arg 라도 보통 tuple 로 감싸지만, 직접 LogRecord 만들 땐 단일 값 가능.
    record = _make_record("msg %s", "password=p4ssw0rd token")
    _RedactFilter().filter(record)
    formatted = _format(record)
    assert "p4ssw0rd" not in formatted
    assert "password=***" in formatted


def test_non_string_arg_passes_through_unmodified() -> None:
    """비문자열 인자(int, dict 객체 등) 는 변형하지 않는다 — 비파괴 보장."""
    record = _make_record("rc=%d count=%d", (42, 7))
    _RedactFilter().filter(record)
    formatted = _format(record)
    assert "rc=42" in formatted
    assert "count=7" in formatted


def test_multiline_stderr_preserves_structure_after_masking() -> None:
    """다중라인 stderr 가 args 로 들어가도 마스킹 후 줄바꿈이 유지돼 가독성 보존."""
    stderr = (
        "OpenAI Codex v0.124.0 (research preview)\n"
        "--------\n"
        "model: gpt-5.5\n"
        "--------\n"
        "Error: authorization=Bearer leak123 invalid\n"
    )
    msg_template = "codex failed:\n%s"
    record = _make_record(msg_template, (stderr,))
    _RedactFilter().filter(record)
    formatted = _format(record)
    assert "leak123" not in formatted
    assert "authorization=***" in formatted
    # 줄바꿈은 그대로 유지돼 운영자가 multi-line 으로 읽을 수 있어야 한다.
    # formatted = "codex failed:\n" + masked stderr → stderr 의 \n 개수 + 1.
    assert formatted.count("\n") == stderr.count("\n") + 1
