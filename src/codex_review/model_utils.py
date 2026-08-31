from collections.abc import Iterable
from typing import Literal, TypeAlias

ReasoningEffort: TypeAlias = Literal["low", "medium", "high", "xhigh", "max", "ultra"]
DEFAULT_CODEX_REASONING_EFFORT: ReasoningEffort = "xhigh"

# Codex CLI 0.144.1 ChatGPT-auth catalog 기준 실제 사용 가능한 입력 윈도우.
# Sol 은 기본 272K보다 큰 확장 윈도우를 지원하므로 max_context_window 값을 사용한다.
_KNOWN_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-5.6-sol": 872_000,
    "gpt-5.5": 272_000,
    "gpt-5.3-codex-spark": 128_000,
}

_STANDARD_REASONING_EFFORTS: frozenset[ReasoningEffort] = frozenset(
    {"low", "medium", "high", "xhigh"}
)
_MAX_REASONING_EFFORTS: frozenset[ReasoningEffort] = frozenset(
    {"low", "medium", "high", "xhigh", "max"}
)
_ULTRA_REASONING_EFFORTS: frozenset[ReasoningEffort] = frozenset(
    {"low", "medium", "high", "xhigh", "max", "ultra"}
)
_KNOWN_MODEL_REASONING_EFFORTS: dict[str, frozenset[ReasoningEffort]] = {
    "gpt-5.6-sol": _ULTRA_REASONING_EFFORTS,
    "gpt-5.6-terra": _ULTRA_REASONING_EFFORTS,
    "gpt-5.6-luna": _MAX_REASONING_EFFORTS,
    "gpt-reserve": _MAX_REASONING_EFFORTS,
    "gpt-5.5": _STANDARD_REASONING_EFFORTS,
    "gpt-5.4": _STANDARD_REASONING_EFFORTS,
    "gpt-5.4-mini": _STANDARD_REASONING_EFFORTS,
    "gpt-5.3-codex-spark": _STANDARD_REASONING_EFFORTS,
    "codex-auto-review": _MAX_REASONING_EFFORTS,
}


def incompatible_reasoning_effort_models(
    models: Iterable[str],
    reasoning_effort: ReasoningEffort,
) -> tuple[str, ...]:
    """Return known models that do not support the configured reasoning effort."""
    return tuple(
        model
        for model in models
        if (supported := _KNOWN_MODEL_REASONING_EFFORTS.get(model)) is not None
        and reasoning_effort not in supported
    )


def dedupe_models(models: Iterable[str]) -> tuple[str, ...]:
    """Return model names in first-seen order, rejecting an empty sequence."""
    if isinstance(models, str):
        raise TypeError("models must be an iterable of model names, not a single string")
    ordered = tuple(dict.fromkeys(models))
    if not ordered:
        raise ValueError("at least one Codex model is required")
    return ordered


def known_model_context_window(model: str) -> int | None:
    """Return the Codex CLI context window known for ``model``, if catalogued."""
    return _KNOWN_MODEL_CONTEXT_WINDOWS.get(model)
