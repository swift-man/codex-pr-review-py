from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, TypeAlias

ReasoningEffort: TypeAlias = Literal["low", "medium", "high", "xhigh", "max", "ultra"]
DEFAULT_CODEX_REASONING_EFFORT: ReasoningEffort = "max"

_STANDARD_REASONING_EFFORTS: frozenset[ReasoningEffort] = frozenset(
    {"low", "medium", "high", "xhigh"}
)
_MAX_REASONING_EFFORTS: frozenset[ReasoningEffort] = frozenset(
    {"low", "medium", "high", "xhigh", "max"}
)
_ULTRA_REASONING_EFFORTS: frozenset[ReasoningEffort] = frozenset(
    {"low", "medium", "high", "xhigh", "max", "ultra"}
)
_REASONING_EFFORT_RANK: dict[ReasoningEffort, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "xhigh": 3,
    "max": 4,
    "ultra": 5,
}


@dataclass(frozen=True, slots=True)
class _KnownModelConfig:
    default_context_window: int
    max_context_window: int
    reasoning_efforts: frozenset[ReasoningEffort]


# Codex CLI 0.144.1 ChatGPT-auth catalog 기준 모델 설정. 프로젝트의 1순위 모델인
# Sol 만 확장 윈도우를 기본 정책으로 선택하고, 나머지는 CLI 기본값을 유지한다.
_KNOWN_MODELS: dict[str, _KnownModelConfig] = {
    "gpt-5.6-sol": _KnownModelConfig(872_000, 872_000, _ULTRA_REASONING_EFFORTS),
    "gpt-6-astra": _KnownModelConfig(272_000, 872_000, _ULTRA_REASONING_EFFORTS),
    "gpt-5.6-terra": _KnownModelConfig(272_000, 872_000, _ULTRA_REASONING_EFFORTS),
    "gpt-5.6-luna": _KnownModelConfig(272_000, 872_000, _MAX_REASONING_EFFORTS),
    "gpt-reserve": _KnownModelConfig(272_000, 872_000, _MAX_REASONING_EFFORTS),
    "gpt-5.5": _KnownModelConfig(272_000, 272_000, _STANDARD_REASONING_EFFORTS),
    "gpt-5.4": _KnownModelConfig(272_000, 1_000_000, _STANDARD_REASONING_EFFORTS),
    "gpt-5.4-mini": _KnownModelConfig(
        272_000,
        272_000,
        _STANDARD_REASONING_EFFORTS,
    ),
    "gpt-5.3-codex-spark": _KnownModelConfig(
        128_000,
        128_000,
        _STANDARD_REASONING_EFFORTS,
    ),
    "codex-auto-review": _KnownModelConfig(272_000, 872_000, _MAX_REASONING_EFFORTS),
}


def effective_reasoning_effort(model: str, requested: ReasoningEffort) -> ReasoningEffort:
    """Choose the strongest supported effort at or below the request when possible."""
    config = _KNOWN_MODELS.get(model)
    if config is None or requested in config.reasoning_efforts:
        return requested

    requested_rank = _REASONING_EFFORT_RANK[requested]
    compatible = (
        effort
        for effort in config.reasoning_efforts
        if _REASONING_EFFORT_RANK[effort] <= requested_rank
    )
    minimum_supported = min(config.reasoning_efforts, key=_REASONING_EFFORT_RANK.__getitem__)
    return max(
        compatible,
        key=_REASONING_EFFORT_RANK.__getitem__,
        default=minimum_supported,
    )


def dedupe_models(models: Iterable[str]) -> tuple[str, ...]:
    """Return model names in first-seen order, rejecting an empty sequence."""
    if isinstance(models, str):
        raise TypeError("models must be an iterable of model names, not a single string")
    ordered = tuple(dict.fromkeys(models))
    if not ordered:
        raise ValueError("at least one Codex model is required")
    return ordered


def known_model_default_context_window(model: str) -> int | None:
    """Return the project default context window for a catalogued Codex model."""
    config = _KNOWN_MODELS.get(model)
    return config.default_context_window if config is not None else None


def known_model_max_context_window(model: str) -> int | None:
    """Return the maximum context window exposed by the Codex CLI catalog."""
    config = _KNOWN_MODELS.get(model)
    return config.max_context_window if config is not None else None
