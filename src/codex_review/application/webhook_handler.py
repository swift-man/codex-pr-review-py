import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass

from codex_review.domain import RepoRef
from codex_review.interfaces import GitHubClient
from codex_review.logging_utils import get_delivery_logger

from .review_pr_use_case import ReviewPullRequestUseCase

logger = logging.getLogger(__name__)

_SUPPORTED_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


@dataclass(frozen=True)
class WebhookJob:
    delivery_id: str
    repo: RepoRef
    number: int
    installation_id: int


class WebhookHandler:
    """Verifies webhooks, enqueues review jobs, drains them with bounded concurrency.

    구조:
      asyncio.Queue <- `accept()` 가 넣고
      N 개의 워커 코루틴 <- 병렬로 꺼내 처리하되, `asyncio.Semaphore(concurrency)` 로
                         동시 실행 수를 제한. 기본 N=1 (직렬) — 필요 시 env 로 상향.
    """

    def __init__(
        self,
        secret: str,
        github: GitHubClient,
        use_case: ReviewPullRequestUseCase,
        concurrency: int = 1,
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._github = github
        self._use_case = use_case
        self._concurrency = max(1, concurrency)
        self._queue: asyncio.Queue[WebhookJob] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._sem = asyncio.Semaphore(self._concurrency)

    # --- Lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._workers:
            return
        # Semaphore 가 동시 실행 상한을 관리하므로 워커 수를 concurrency 와 맞춰 만들면
        # "큐에서 꺼내는 일" 과 "실제 실행" 이 모두 병렬 가능.
        for i in range(self._concurrency):
            task = asyncio.create_task(self._run(), name=f"review-worker-{i}")
            self._workers.append(task)
        logger.info("webhook handler started with concurrency=%d", self._concurrency)

    async def stop(self) -> None:
        # 서버 종료 시 큐에 남은 작업 없이 깔끔히 마무리하도록 워커를 취소한다.
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._workers.clear()
        logger.info("webhook handler stopped")

    # --- Verification -------------------------------------------------------

    def verify_signature(self, signature_header: str | None, body: bytes) -> bool:
        # 원문 body 로 HMAC 계산. json.loads 후 재직렬화하면 서명이 달라져 정상 요청을 거부.
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature_header.removeprefix("sha256="), expected)

    # --- Dispatch -----------------------------------------------------------

    async def accept(
        self,
        event: str,
        delivery_id: str,
        payload: dict,
    ) -> tuple[int, str]:
        dlog = get_delivery_logger(__name__, delivery_id)
        if event == "ping":
            return 200, "pong"
        if event != "pull_request":
            dlog.info("ignoring event: %s", event)
            return 202, "ignored"

        action = str(payload.get("action", ""))
        if action not in _SUPPORTED_ACTIONS:
            dlog.info("ignoring action: %s", action)
            return 202, "ignored-action"

        pr = payload.get("pull_request") or {}
        # webhook payload 의 draft 값과 실제 처리 시점 상태가 다를 수 있어 _process 에서 재확인.
        if bool(pr.get("draft")):
            dlog.info("skipping draft PR")
            return 202, "skipped-draft"

        repo_full = str(payload.get("repository", {}).get("full_name", ""))
        if "/" not in repo_full:
            dlog.warning("missing repository full_name in payload")
            return 400, "invalid-payload"
        owner, name = repo_full.split("/", 1)

        number = int(pr.get("number", 0))
        installation_id = int(payload.get("installation", {}).get("id", 0))
        if number == 0 or installation_id == 0:
            dlog.warning("missing number=%s or installation_id=%s", number, installation_id)
            return 400, "invalid-payload"

        job = WebhookJob(
            delivery_id=delivery_id,
            repo=RepoRef(owner=owner, name=name),
            number=number,
            installation_id=installation_id,
        )
        await self._queue.put(job)
        dlog.info(
            "queued review for %s#%d (queue_depth=%d)",
            job.repo.full_name,
            job.number,
            self._queue.qsize(),
        )
        return 202, "queued"

    # --- Worker -------------------------------------------------------------

    async def _run(self) -> None:
        # 동시성 상한은 Semaphore 로 통제. 모든 워커가 같은 세마포어를 공유하므로 워커 수와
        # 무관하게 순간 병렬 실행 수는 `concurrency` 를 넘지 않는다.
        while True:
            job = await self._queue.get()
            try:
                async with self._sem:
                    await self._process(job)
            finally:
                self._queue.task_done()

    async def _process(self, job: WebhookJob) -> None:
        dlog = get_delivery_logger(__name__, job.delivery_id)
        try:
            dlog.info("processing %s#%d", job.repo.full_name, job.number)
            pr = await self._github.fetch_pull_request(
                job.repo, job.number, job.installation_id
            )
            if pr.is_draft:
                dlog.info("skipping draft at fetch time")
                return
            await self._use_case.execute(pr)
            dlog.info("done %s#%d", job.repo.full_name, job.number)
        except asyncio.CancelledError:
            raise
        except Exception:
            dlog.exception("review failed for %s#%d", job.repo.full_name, job.number)
