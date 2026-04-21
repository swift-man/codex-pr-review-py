import asyncio
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from codex_review.domain import PullRequest

logger = logging.getLogger(__name__)


class GitRepoFetcher:
    """Async git wrapper. Clones or updates a cached repo and checks out a PR head SHA."""

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir

    async def checkout(self, pr: PullRequest, installation_token: str) -> Path:
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

        # depth=1 로 head SHA 만 얕게 받아 네트워크/디스크 비용 최소화. 리뷰엔 이 정도로 충분.
        await _run(["git", "-C", str(repo_path), "fetch", "--depth", "1", "origin", pr.head_sha])
        # --force: 이전 리뷰에서 남은 local modification 이 있어도 무시하고 대상 SHA 로 전환.
        await _run(["git", "-C", str(repo_path), "checkout", "--force", pr.head_sha])
        # -fdx: 추적 안되는 파일/디렉터리/ignore 대상까지 전부 제거해 이전 체크아웃 잔여물 배제.
        await _run(["git", "-C", str(repo_path), "clean", "-fdx"])

        # 디스크에 저장된 .git/config 에 토큰이 남지 않도록 remote URL 을 원래 값으로 복구.
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


async def _run(cmd: list[str], *, check: bool = True) -> None:
    logger.debug("git %s", " ".join(cmd[1:]))
    # stdout 은 소비하지 않으므로 DEVNULL 로 보내 파이프 버퍼링/메모리 오버헤드를 피한다.
    # stderr 는 실패 시 메시지 확인용이라 PIPE 로 유지.
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
