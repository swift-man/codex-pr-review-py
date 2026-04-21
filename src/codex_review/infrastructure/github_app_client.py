import asyncio
import logging
import ssl
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import certifi
import httpx
import jwt

from codex_review.domain import Finding, PullRequest, RepoRef, ReviewEvent, ReviewResult

from .diff_parser import parse_right_lines

logger = logging.getLogger(__name__)


def _default_tls_context() -> ssl.SSLContext:
    """macOS · python.org 빌드 Python 은 시스템 CA 번들을 자동으로 신뢰하지 않아
    https 호출 시 CERTIFICATE_VERIFY_FAILED 가 뜬다. certifi 번들을 명시한다.
    """
    return ssl.create_default_context(cafile=certifi.where())


# 리뷰 본문 footer 포맷. 모델명은 `CODEX_MODEL` 값을 그대로 표시한다.
_MODEL_FOOTER_TEMPLATE = "\n\n---\n<sub>리뷰 모델: <code>{label}</code></sub>"


def _with_model_footer(body: str, model_label: str | None) -> str:
    if not model_label:
        return body
    return body + _MODEL_FOOTER_TEMPLATE.format(label=model_label)


@dataclass(frozen=True)
class _CachedToken:
    token: str
    expires_at: float

    def is_valid(self) -> bool:
        return time.time() < self.expires_at - 60


class GitHubAppClient:
    """Async GitHub REST client authenticating as a GitHub App installation.

    httpx.AsyncClient 를 공유하며 수명 주기는 외부에서 관리한다(lifespan). 테스트에서는
    `transport=httpx.MockTransport(...)` 를 주입해 네트워크 없이 검증.
    """

    def __init__(
        self,
        app_id: int,
        private_key_pem: str,
        http_client: httpx.AsyncClient,
        dry_run: bool = False,
        review_model_label: str | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key_pem
        self._http = http_client
        self._dry_run = dry_run
        # 본문 footer 에 표시할 모델 라벨. None 이면 footer 생략.
        self._review_model_label = review_model_label
        self._token_cache: dict[int, _CachedToken] = {}
        # 동일 installation token 캐시 동시 갱신 방지용 락. 동시성 N>1 상황에서 두 워커가
        # 같은 만료 토큰을 동시에 재발급하면 네트워크 낭비 + 경쟁 조건.
        self._token_lock = asyncio.Lock()

    # --- Auth ---------------------------------------------------------------

    def _app_jwt(self) -> str:
        # iat 를 30초 과거로 당기고 exp 를 10분 한도(GitHub 제한)에 못 미치는 9분으로 잡는 건
        # 로컬-GitHub 간 시계 오차로 인한 "JWT not yet valid / expired" 실패를 피하기 위함.
        now = int(time.time())
        payload = {"iat": now - 30, "exp": now + 9 * 60, "iss": str(self._app_id)}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def get_installation_token(self, installation_id: int) -> str:
        cached = self._token_cache.get(installation_id)
        if cached and cached.is_valid():
            return cached.token

        async with self._token_lock:
            # lock 진입 후 재확인: 대기 중 다른 워커가 이미 갱신했을 수 있다.
            cached = self._token_cache.get(installation_id)
            if cached and cached.is_valid():
                return cached.token

            data = await self._request(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                auth=f"Bearer {self._app_jwt()}",
            )
            token = str(data["token"])
            expires = str(data.get("expires_at", ""))
            # GitHub installation token 은 1시간 유효. 만료 직전 요청이 실패하지 않도록 5분 여유.
            expires_at = time.time() + 55 * 60
            if expires:
                # GitHub 은 expires_at 을 UTC (..Z) 로 반환. time.mktime 경로는 로컬
                # TZ 기준이라 오프셋만큼 어긋나므로 aware datetime 으로 파싱.
                try:
                    expires_at = datetime.fromisoformat(
                        expires.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    pass
            self._token_cache[installation_id] = _CachedToken(token, expires_at)
            return token

    # --- Public API ---------------------------------------------------------

    async def fetch_pull_request(
        self, repo: RepoRef, number: int, installation_id: int
    ) -> PullRequest:
        token = await self.get_installation_token(installation_id)
        pr_path = f"/repos/{repo.full_name}/pulls/{number}"
        pr_data = await self._request("GET", pr_path, auth=f"token {token}")
        assert isinstance(pr_data, dict)

        # 변경 파일 전체를 가져와야 우선순위 정렬(변경 파일 먼저)이 정확해진다.
        # per_page=100 은 GitHub 허용 최대치라 PR 이 큰 경우의 라운드트립 수를 최소화.
        changed: list[str] = []
        diff_right_lines: dict[str, frozenset[int]] = {}
        page = 1
        while True:
            files = await self._request(
                "GET",
                f"{pr_path}/files?per_page=100&page={page}",
                auth=f"token {token}",
            )
            if not isinstance(files, list) or not files:
                break
            for f in files:
                path = str(f["filename"])
                changed.append(path)
                # GitHub 는 큰 diff / rename / delete / binary 상태에서 `patch` 키를 생략한다.
                # 그 파일에 대한 인라인 코멘트는 use-case 필터에서 전부 사라지므로 운영자가
                # 알아볼 수 있도록 경고 로그로 남긴다.
                patch = f.get("patch")
                if patch is None:
                    logger.warning(
                        "GitHub omitted patch for %s#%d file %r (status=%s); "
                        "inline comments on this file will be suppressed",
                        repo.full_name,
                        number,
                        path,
                        f.get("status"),
                    )
                diff_right_lines[path] = parse_right_lines(patch)
            # 100개 미만이면 마지막 페이지 — Link 헤더 대신 길이로 단순 판정.
            if len(files) < 100:
                break
            page += 1

        head = pr_data["head"]
        base = pr_data["base"]
        return PullRequest(
            repo=repo,
            number=number,
            title=str(pr_data.get("title", "")),
            body=str(pr_data.get("body") or ""),
            head_sha=str(head["sha"]),
            head_ref=str(head["ref"]),
            base_sha=str(base["sha"]),
            base_ref=str(base["ref"]),
            clone_url=str(head["repo"]["clone_url"]),
            changed_files=tuple(changed),
            installation_id=installation_id,
            is_draft=bool(pr_data.get("draft", False)),
            diff_right_lines=diff_right_lines,
        )

    async def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        if self._dry_run:
            logger.info("DRY_RUN — review not posted: %s#%d", pr.repo.full_name, pr.number)
            return

        token = await self.get_installation_token(pr.installation_id)
        path = f"/repos/{pr.repo.full_name}/pulls/{pr.number}/reviews"

        # commit_id 를 명시해야 리뷰가 "이 head SHA 시점"에 고정된다. 생략하면 최신 SHA 기준으로
        # 붙어 라인 번호 오정렬이 발생할 수 있다.
        payload: dict[str, object] = {
            "commit_id": pr.head_sha,
            "body": _with_model_footer(result.render_body(), self._review_model_label),
            "event": result.event.value,
            "comments": [_finding_to_comment(f) for f in result.findings],
        }
        try:
            await self._request("POST", path, auth=f"token {token}", body=payload)
        except httpx.HTTPStatusError as exc:
            # 방어선: use-case 단계의 diff 필터가 있음에도 422 가 나면 인라인 코멘트를 포기하고
            # 본문만 재게시한다. 리뷰 전체를 포기하는 것보다 낫다.
            # 본문도 findings 제거된 상태로 재렌더해야 "기술 단위 코멘트 N건" 안내가 남는
            # 거짓 상태를 피할 수 있다.
            if exc.response.status_code == 422 and payload["comments"]:
                logger.warning(
                    "422 on review POST for %s#%d; retrying without inline comments",
                    pr.repo.full_name,
                    pr.number,
                )
                retry_result = replace(result, findings=())
                payload["body"] = _with_model_footer(
                    retry_result.render_body(), self._review_model_label
                )
                payload["comments"] = []
                await self._request("POST", path, auth=f"token {token}", body=payload)
            else:
                raise

    async def post_comment(self, pr: PullRequest, body: str) -> None:
        if self._dry_run:
            logger.info("DRY_RUN — comment not posted: %s#%d", pr.repo.full_name, pr.number)
            return

        token = await self.get_installation_token(pr.installation_id)
        path = f"/repos/{pr.repo.full_name}/issues/{pr.number}/comments"
        await self._request("POST", path, auth=f"token {token}", body={"body": body})

    # --- HTTP ---------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth: str,
        body: object | None = None,
    ) -> Any:
        """Issue a single JSON REST call. `path` 는 base_url 에 붙는 상대 경로."""
        headers = {
            "Authorization": auth,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codex-review-bot",
        }
        resp = await self._http.request(method, path, headers=headers, json=body)
        # httpx 는 4xx/5xx 를 예외로 승격시키지 않으므로 명시적으로 raise — `post_review` 가
        # 422 를 구분해서 잡아야 하기 때문에 HTTPStatusError 가 필요.
        if resp.status_code >= 400:
            logger.error(
                "GitHub %s %s failed: %s %s",
                method, path, resp.status_code, resp.text[:500],
            )
            raise httpx.HTTPStatusError(
                f"{resp.status_code} {resp.reason_phrase}",
                request=resp.request,
                response=resp,
            )
        if not resp.content:
            return {}
        return resp.json()


def _finding_to_comment(f: Finding) -> dict[str, object]:
    return {"path": f.path, "line": f.line, "side": "RIGHT", "body": f.body}


__all__ = ["GitHubAppClient", "ReviewEvent", "_default_tls_context"]
