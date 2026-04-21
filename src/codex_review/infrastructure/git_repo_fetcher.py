import asyncio
import logging
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from codex_review.domain import PullRequest

logger = logging.getLogger(__name__)


# 같은 저장소 캐시를 병렬 리뷰가 동시에 건드리면 fetch/checkout/clean 이 섞여
# 다른 PR 의 head SHA 가 checkout 되거나 작업 트리가 중간 상태로 남는다.
# 저장소 단위로 직렬화해 이 경쟁을 제거한다. 상한은 현실적 규모의 10배 여유.
_MAX_TRACKED_REPOS = 256


class _RepoLockRegistry:
    """owner/repo → `asyncio.Lock` 매핑을 LRU 상한으로 관리."""

    def __init__(self, maxsize: int = _MAX_TRACKED_REPOS) -> None:
        self._maxsize = maxsize
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()

    def get(self, full_name: str) -> asyncio.Lock:
        lock = self._locks.get(full_name)
        if lock is not None:
            self._locks.move_to_end(full_name)
            return lock
        while len(self._locks) >= self._maxsize:
            self._locks.popitem(last=False)
        lock = asyncio.Lock()
        self._locks[full_name] = lock
        return lock


class GitRepoFetcher:
    """Async git wrapper. Clones or updates a cached repo and checks out a PR head SHA.

    저장소별 `asyncio.Lock` 으로 동일 저장소의 checkout 이 직렬화된다 —
    REVIEW_CONCURRENCY≥2 환경에서 같은 레포에 여러 PR 리뷰가 동시에 들어와도
    작업 트리가 엉키지 않는다.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._repo_locks = _RepoLockRegistry()

    async def checkout(self, pr: PullRequest, installation_token: str) -> Path:
        async with self._repo_locks.get(pr.repo.full_name):
            return await self._checkout_locked(pr, installation_token)

    async def _checkout_locked(self, pr: PullRequest, installation_token: str) -> Path:
        repo_path = self._cache_dir / pr.repo.owner / pr.repo.name
        repo_path.parent.mkdir(parents=True, exist_ok=True)

        authed_url = _inject_token(pr.clone_url, installation_token)

        if not (repo_path / ".git").exists():
            logger.info("cloning %s into %s", pr.repo.full_name, repo_path)
            # --filter=blob:none 은 partial clone — 블롭을 지연 로드해 초기 clone 속도·디스크 절약.
            await _run(["git", "clone", "--filter=blob:none", authed_url, str(repo_path)])
        else:
            # 설치 토큰은 1시간마다 바뀌므로 기존 remote URL 의 토큰을 교체해야 fetch 가 성공.
            await _run(["git", "-C", str(repo_path), "remote", "set-url", "origin", authed_url])

        try:
            # depth=1 로 head SHA 만 얕게 받아 네트워크/디스크 비용 최소화.
            await _run(
                ["git", "-C", str(repo_path), "fetch", "--depth", "1", "origin", pr.head_sha]
            )
            # --force: 이전 리뷰에서 남은 local modification 이 있어도 무시하고 대상 SHA 로 전환.
            await _run(["git", "-C", str(repo_path), "checkout", "--force", pr.head_sha])
            # -fdx: 추적 안되는 파일/디렉터리/ignore 대상까지 전부 제거.
            await _run(["git", "-C", str(repo_path), "clean", "-fdx"])
        finally:
            # 예외가 나든 나지 않든 remote URL 을 원래 값으로 반드시 복구.
            # 그러지 않으면 `.git/config` 에 installation 토큰이 남아 디스크에 자격 증명이 보존된다.
            await _run(
                ["git", "-C", str(repo_path), "remote", "set-url", "origin", pr.clone_url],
                check=False,
            )
        return repo_path


def _inject_token(clone_url: str, token: str) -> str:
    # GitHub 권장: username=x-access-token, password=installation token.
    parts = urlsplit(clone_url)
    netloc = f"x-access-token:{token}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _mask_token_in_url(value: str) -> str:
    """`https://x-access-token:<tok>@host/path` → `https://***@host/path`.

    디버그 로그에 원문을 찍으면 커맨드라인 인자가 그대로 남아 토큰이 유출된다.
    `logging_utils._SECRET_PATTERN` 은 `record.args` 를 마스킹하지 못하므로 **기록 직전**
    에 마스킹해야 한다.
    """
    parts = urlsplit(value)
    if parts.scheme in ("http", "https") and parts.username:
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, f"***@{host}", parts.path, parts.query, parts.fragment))
    return value


async def _run(cmd: list[str], *, check: bool = True) -> None:
    # 기록 직전 토큰이 포함된 URL 을 마스킹한다 (URL 형태가 아니면 원본 그대로).
    masked = [_mask_token_in_url(arg) for arg in cmd[1:]]
    logger.debug("git %s", " ".join(masked))
    # stdout 은 소비하지 않으므로 DEVNULL 로 — 파이프 버퍼링/메모리 오버헤드 제거.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git command failed ({proc.returncode}): {' '.join(cmd[:2])}...\n"
            f"{stderr.decode(errors='replace').strip()}"
        )
