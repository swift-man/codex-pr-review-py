"""Regression coverage for the httpx.AsyncClient wiring in GitHubAppClient.

이전에는 urllib 에 certifi CA 번들을 주입하는 흐름이 핵심이었다. 지금은
httpx.AsyncClient 생성 시 `verify=_default_tls_context()` 를 넘기면 되며,
이 파일은 "certifi 번들이 실제로 SSLContext 의 cafile 로 전달되는지" 를 고정한다.
"""

import ssl
from collections.abc import Iterator

import httpx
import jwt
import pytest

from codex_review.infrastructure import github_app_client


def test_default_tls_context_uses_certifi_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """회귀 방지: `_default_tls_context` 가 `certifi.where()` 결과를 `cafile` 로 넘긴다.
    누군가 certifi 호출을 제거해도 정확히 이 테스트가 실패하도록 훅을 건다.
    """
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        github_app_client.certifi, "where", lambda: "/fake/certifi/bundle.pem"
    )
    original = github_app_client.ssl.create_default_context

    def spy(*args: object, cafile: str | None = None, **kwargs: object) -> ssl.SSLContext:
        captured["cafile"] = cafile
        # 유효한 컨텍스트를 돌려줘야 이후 로직이 깨지지 않음. 단, 가짜 cafile 경로라
        # 실제 신뢰할 수 있는 컨텍스트는 못 만들므로 certifi 검증용 호출을 제외하고 원본 호출.
        return original()

    monkeypatch.setattr(
        github_app_client.ssl, "create_default_context", spy
    )

    github_app_client._default_tls_context()
    assert captured["cafile"] == "/fake/certifi/bundle.pem"


@pytest.fixture()
def mock_http() -> Iterator[httpx.AsyncClient]:
    """요청/응답을 캡처해 테스트가 호출 경로를 검증할 수 있게 하는 MockTransport 기반 클라이언트."""
    def handler(req: httpx.Request) -> httpx.Response:
        # 기본 응답: 토큰 발급 응답만 돌려주고 나머지는 빈 객체.
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(
                200, json={"token": "ITOK", "expires_at": "2026-04-22T00:00:00Z"}
            )
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://api.github.com", transport=transport)
    try:
        yield client
    finally:
        import asyncio
        asyncio.get_event_loop().run_until_complete(client.aclose())


async def test_request_uses_injected_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DIP 검증: GitHubAppClient 가 주입된 httpx.AsyncClient 로만 요청한다.
    테스트 transport 가 기록한 경로·헤더를 본다.
    """
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200, json={"token": "ITOK", "expires_at": "2026-04-22T00:00:00Z"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=transport
    ) as http_client:
        monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
        client = github_app_client.GitHubAppClient(
            app_id=1, private_key_pem="-", http_client=http_client
        )

        token = await client.get_installation_token(installation_id=7)

    assert token == "ITOK"
    assert captured, "request should have been issued"
    assert captured[0].url.path == "/app/installations/7/access_tokens"
    assert captured[0].headers["Accept"] == "application/vnd.github+json"
    assert captured[0].headers["Authorization"].startswith("Bearer ")
