from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, StringConstraints, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from codex_review.model_utils import (
    DEFAULT_CODEX_REASONING_EFFORT,
    ReasoningEffort,
    dedupe_models,
    incompatible_reasoning_effort_models,
    known_model_context_window,
)

# 공백만으로 이뤄진 시크릿·호스트·모델명을 차단 — 빈 문자열뿐 아니라 `"   "` 도 거절해야
# HMAC 무력화·바인딩 실패 같은 조용한 설정 사고를 기동 단계에서 막을 수 있다 (codex 리뷰).
# `strip_whitespace=True` 로 주변 공백을 제거한 뒤 `min_length=1` 을 평가한다.
NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
_DEFAULT_CODEX_MODEL_FALLBACKS = "gpt-5.5,gpt-5.3-codex-spark"
_DEFAULT_CODEX_MAX_INPUT_TOKENS = 828_400
_CONTEXT_WINDOW_BUDGET_PERCENT = 95


class Settings(BaseSettings):
    """환경 변수 기반 서버 설정.

    `Field(..., gt=0)` / `le=…` 형태로 기본 제약을 걸어 운영자가 `REVIEW_CONCURRENCY=0`
    같은 무효한 값을 넣었을 때 핸들러에서 조용히 `max(1, …)` 로 보정되는 대신 기동
    시점에 `ValidationError` 로 즉시 실패하게 한다. 문제의 원인을 이른 시점에 드러내
    야 운영 사고(쿼터 폭주·무한 대기 등) 를 피할 수 있다 — codex 본문 피드백 반영.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GitHub App
    github_app_id: int = Field(..., gt=0, alias="GITHUB_APP_ID")
    github_app_private_key_path: Path | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY_PATH"
    )
    github_app_private_key: NonBlankStr | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY"
    )
    github_webhook_secret: NonBlankStr = Field(..., alias="GITHUB_WEBHOOK_SECRET")
    github_api_base: str = Field(default="https://api.github.com", alias="GITHUB_API_BASE")
    # PR 댓글 follow-up 기능 활성화에 필요한 봇 슬러그 (예: "codex-review-bot").
    # GitHub 가 게시한 본인 댓글의 `user.login` 은 `f"{slug}[bot]"` 형태이므로, 이 값으로
    # 우리 봇이 단 코멘트만 골라 follow-up 한다. 미설정 (None) 이면 follow-up 기능 자체
    # 비활성화 — 운영자가 슬러그를 알고 명시적으로 옵트인 해야 작동한다.
    github_app_slug: NonBlankStr | None = Field(default=None, alias="GITHUB_APP_SLUG")

    # Codex CLI — 음수/0 타임아웃이나 토큰 한도는 리뷰를 즉시 실패시키므로 `gt=0` 로 고정.
    codex_bin: str = Field(default="codex", alias="CODEX_BIN")
    codex_model: NonBlankStr = Field(default=_DEFAULT_CODEX_MODEL, alias="CODEX_MODEL")
    codex_fallback_models: str = Field(
        default=_DEFAULT_CODEX_MODEL_FALLBACKS, alias="CODEX_MODEL_FALLBACKS"
    )
    codex_reasoning_effort: ReasoningEffort = Field(
        default=DEFAULT_CODEX_REASONING_EFFORT,
        alias="CODEX_REASONING_EFFORT",
    )
    # 1순위 모델에만 전달할 Codex CLI `model_context_window` 오버라이드. 미설정 시
    # 알려진 모델은 로컬 CLI 카탈로그 값을 사용하고, 사용자 정의 모델은 CLI 기본값을 따른다.
    codex_model_context_window: int | None = Field(
        default=None,
        gt=0,
        alias="CODEX_MODEL_CONTEXT_WINDOW",
    )
    codex_timeout_sec: int = Field(default=600, gt=0, alias="CODEX_TIMEOUT_SEC")
    codex_max_input_tokens: int = Field(
        default=_DEFAULT_CODEX_MAX_INPUT_TOKENS,
        gt=0,
        alias="CODEX_MAX_INPUT_TOKENS",
    )
    # 예산 초과 시 diff-only 모드 자동 fallback 활성화 여부 (기본 True).
    # False 로 내리면 기존 "리뷰 스킵 + 안내 코멘트" 경로만 남는다 — 리뷰 품질을
    # 보수적으로 보장하고 싶은 운영 환경 대비 옵트아웃. (gemini PR #17 제안)
    enable_diff_fallback: bool = Field(default=True, alias="CODEX_ENABLE_DIFF_FALLBACK")

    # Repo / files
    repo_cache_dir: Path = Field(
        default=Path.home() / ".codex-review" / "repos", alias="REPO_CACHE_DIR"
    )
    git_timeout_sec: int = Field(default=120, gt=0, alias="GIT_TIMEOUT_SEC")
    file_max_bytes: int = Field(default=204_800, gt=0, alias="FILE_MAX_BYTES")
    data_file_max_bytes: int = Field(default=20_000, gt=0, alias="DATA_FILE_MAX_BYTES")

    # Server — 포트는 TCP 유효 범위(1–65535) 로 제한.
    host: NonBlankStr = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8000, ge=1, le=65535, alias="PORT")
    dry_run: bool = Field(default=False, alias="DRY_RUN")
    # 동시에 처리할 리뷰 최대 개수. 1 이면 직렬. 2~ 로 올리면 병렬 처리. 0/음수는 의미 없음.
    review_concurrency: int = Field(default=1, gt=0, alias="REVIEW_CONCURRENCY")
    # 웹훅 큐 상한. None 이면 `review_concurrency * 10` 으로 자동 계산. 가득 차면 503 반환.
    # pydantic V2 는 `int | None` 타입에 `gt=0` 을 걸어도 None 은 검증을 건너뛰므로 별도
    # validator 없이 "None 이거나 양수" 계약이 자동 적용된다 (gemini 리뷰).
    review_queue_maxsize: int | None = Field(default=None, gt=0, alias="REVIEW_QUEUE_MAXSIZE")

    @field_validator("github_app_private_key_path", mode="before")
    @classmethod
    def reject_blank_private_key_path(cls, value: object) -> object:
        """Reject empty or whitespace-only private key paths before Path conversion."""
        if isinstance(value, (str, Path)) and not str(value).strip():
            raise ValueError("GITHUB_APP_PRIVATE_KEY_PATH는 공백일 수 없습니다.")
        return value

    @field_validator("codex_reasoning_effort", mode="before")
    @classmethod
    def normalize_codex_reasoning_effort(cls, value: object) -> object:
        """Normalize env input before the Literal contract validates it."""
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_compatible_model_reasoning_effort(self) -> Self:
        """Reject known model sequences that cannot preserve fallback behavior."""
        incompatible = incompatible_reasoning_effort_models(
            self.codex_model_sequence,
            self.codex_reasoning_effort,
        )
        if incompatible:
            models = ", ".join(incompatible)
            raise ValueError(
                f"CODEX_REASONING_EFFORT='{self.codex_reasoning_effort}'은 다음 모델에서 "
                f"지원되지 않습니다: {models}. 모든 모델이 지원하는 값을 사용하거나 "
                "CODEX_MODEL_FALLBACKS를 조정하세요."
            )
        return self

    @model_validator(mode="after")
    def require_input_budget_within_primary_context(self) -> Self:
        """Keep the collector budget inside the primary model's usable context."""
        context_window = self.effective_codex_model_context_window
        if context_window is None:
            return self
        max_safe_tokens = context_window * _CONTEXT_WINDOW_BUDGET_PERCENT // 100
        if self.codex_max_input_tokens > max_safe_tokens:
            raise ValueError(
                f"CODEX_MAX_INPUT_TOKENS={self.codex_max_input_tokens}은 "
                f"{self.codex_model}의 유효 입력 한도 {max_safe_tokens}을 초과합니다. "
                "CODEX_MAX_INPUT_TOKENS를 낮추거나 CODEX_MODEL_CONTEXT_WINDOW를 "
                "실제 모델 한도에 맞게 조정하세요."
            )
        return self

    @model_validator(mode="after")
    def require_single_private_key_source(self) -> Self:
        """Require exactly one GitHub App private key source."""
        if self.github_app_private_key is None and self.github_app_private_key_path is None:
            raise ValueError(
                "GITHUB_APP_PRIVATE_KEY 또는 GITHUB_APP_PRIVATE_KEY_PATH 중 하나가 필요합니다."
            )
        if self.github_app_private_key is not None and self.github_app_private_key_path is not None:
            raise ValueError(
                "GITHUB_APP_PRIVATE_KEY와 GITHUB_APP_PRIVATE_KEY_PATH를 동시에 설정할 수 없습니다."
            )
        return self

    def load_private_key(self) -> str:
        if self.github_app_private_key is not None:
            return self.github_app_private_key
        assert self.github_app_private_key_path is not None
        return self.github_app_private_key_path.read_text(encoding="utf-8")

    @property
    def codex_model_fallbacks(self) -> tuple[str, ...]:
        return _split_model_list(self.codex_fallback_models)

    @property
    def codex_model_sequence(self) -> tuple[str, ...]:
        return dedupe_models((self.codex_model, *self.codex_model_fallbacks))

    @property
    def codex_model_label(self) -> str:
        return " -> ".join(self.codex_model_sequence)

    @property
    def effective_codex_model_context_window(self) -> int | None:
        if self.codex_model_context_window is not None:
            return self.codex_model_context_window
        return known_model_context_window(self.codex_model)


def _split_model_list(raw: str) -> tuple[str, ...]:
    return tuple(part for part in (item.strip() for item in raw.split(",")) if part)
