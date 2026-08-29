from collections.abc import Iterable
from typing import Literal, TypeAlias

ReasoningEffort: TypeAlias = Literal["low", "medium", "high", "xhigh", "max", "ultra"]
DEFAULT_CODEX_REASONING_EFFORT: ReasoningEffort = "xhigh"

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
