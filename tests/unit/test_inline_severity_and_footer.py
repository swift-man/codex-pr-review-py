"""Tests for severity-based inline prefix + model footer preservation."""

import json
import urllib.request
from typing import Any

import jwt
import pytest

from codex_review.domain import (
    Finding,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
)
from codex_review.domain.finding import SEVERITY_MUST_FIX, SEVERITY_SUGGEST
from codex_review.infrastructure.github_app_client import (
    GitHubAppClient,
    _finding_to_comment,
)


def test_finding_to_comment_prefixes_must_fix_body() -> None:
    body = _finding_to_comment(
        Finding(path="a.py", line=1, body="None 체크 누락", severity=SEVERITY_MUST_FIX)
    )
    assert body["body"].startswith("🔴 **반드시 수정**")
    assert "None 체크 누락" in body["body"]


def test_finding_to_comment_leaves_suggest_body_unprefixed() -> None:
    body = _finding_to_comment(
        Finding(path="a.py", line=1, body="pathlib.Path 사용 고려", severity=SEVERITY_SUGGEST)
    )
    assert body["body"] == "pathlib.Path 사용 고려"


def _pr() -> PullRequest:
    return PullRequest(
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
        diff_right_lines={"a.py": frozenset({10})},
    )


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._b = body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def read(self) -> bytes:
        return self._b


def test_post_review_sends_severity_prefixed_comments_and_model_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회귀 방지: severity 접두 + footer 가 실제 POST 페이로드에 함께 들어간다."""
    calls: list[dict[str, Any]] = []

    def fake_urlopen(req, *, timeout=None, context=None):
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        calls.append({"body": body})
        return _FakeResp(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(
        app_id=1, private_key_pem="-", review_model_label="gpt-5.4"
    )
    monkeypatch.setattr(client, "get_installation_token", lambda _iid: "ITOK")

    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.REQUEST_CHANGES,
        must_fix=("핵심 보안 결함",),
        findings=(
            Finding(
                path="a.py", line=10, body="덮어쓰기 경쟁",
                severity=SEVERITY_MUST_FIX,
            ),
        ),
    )
    client.post_review(_pr(), result)

    posted = calls[0]["body"]

    # (1) 본문 섹션: 반드시 수정 + footer
    assert "**🔴 반드시 수정할 사항**" in posted["body"]
    assert posted["body"].rstrip().endswith("<code>gpt-5.4</code></sub>")

    # (2) 인라인 코멘트: severity 접두 적용
    assert len(posted["comments"]) == 1
    assert posted["comments"][0]["body"].startswith("🔴 **반드시 수정**")
