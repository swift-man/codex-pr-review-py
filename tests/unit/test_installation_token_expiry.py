"""Regression coverage for GitHub installation token expiry parsing.

이전 구현은 `time.mktime(time.strptime(..., "%Y-%m-%dT%H:%M:%SZ"))` 를 썼는데
이 조합은 UTC 로 파싱되지 않고 로컬 타임존 기준으로 초가 계산되어 만료 시각이
타임존 오프셋(예: KST 에서 -9시간) 만큼 어긋나는 버그가 있었다.
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any

import jwt
import pytest

from codex_review.infrastructure.github_app_client import GitHubAppClient


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _make_client(monkeypatch: pytest.MonkeyPatch, expires_at_iso: str) -> GitHubAppClient:
    response_body = json.dumps({"token": "TK", "expires_at": expires_at_iso}).encode("utf-8")

    def fake_urlopen(req, *, timeout=None, context=None):
        return _FakeResp(response_body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
    return GitHubAppClient(app_id=1, private_key_pem="-")


def _cached_expires_at(client: GitHubAppClient, installation_id: int) -> float:
    # 테스트 전용: 캐시 엔트리 확인. 공개 속성이 없어 내부 필드를 참조한다.
    return client._token_cache[installation_id].expires_at  # type: ignore[attr-defined]


def test_expires_at_parsed_as_utc_regardless_of_local_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    """로컬 TZ 를 어떤 값으로 바꿔도 동일한 UTC 문자열은 동일한 epoch 로 변환돼야 한다."""
    iso = "2026-04-22T00:00:00Z"
    expected_epoch = datetime(2026, 4, 22, 0, 0, 0, tzinfo=timezone.utc).timestamp()

    # (1) 로컬 TZ = UTC
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    client = _make_client(monkeypatch, iso)
    client.get_installation_token(installation_id=7)
    got_utc = _cached_expires_at(client, 7)

    # (2) 로컬 TZ = KST (UTC+9)
    monkeypatch.setenv("TZ", "Asia/Seoul")
    time.tzset()
    client = _make_client(monkeypatch, iso)
    client.get_installation_token(installation_id=7)
    got_kst = _cached_expires_at(client, 7)

    # (3) 로컬 TZ = US/Pacific (UTC-8/-7)
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    client = _make_client(monkeypatch, iso)
    client.get_installation_token(installation_id=7)
    got_pacific = _cached_expires_at(client, 7)

    # 세 결과 모두 동일 UTC epoch 로 수렴해야 한다.
    assert got_utc == pytest.approx(expected_epoch, abs=1.0)
    assert got_kst == pytest.approx(expected_epoch, abs=1.0)
    assert got_pacific == pytest.approx(expected_epoch, abs=1.0)

    # cleanup: 원래 TZ 로 복구 (fixture 범위)
    if "TZ" in os.environ:
        del os.environ["TZ"]
    time.tzset()


def test_invalid_expires_at_falls_back_to_55min_default(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, "not-a-real-timestamp")
    before = time.time()
    client.get_installation_token(installation_id=7)
    got = _cached_expires_at(client, 7)

    # 5분 여유가 걸린 55분 뒤 기본값이 쓰여야 한다.
    expected = before + 55 * 60
    assert abs(got - expected) < 5.0


def test_empty_expires_at_falls_back_to_55min_default(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, "")
    before = time.time()
    client.get_installation_token(installation_id=7)
    got = _cached_expires_at(client, 7)

    expected = before + 55 * 60
    assert abs(got - expected) < 5.0
