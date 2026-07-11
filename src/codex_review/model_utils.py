from collections.abc import Iterable


def dedupe_models(models: Iterable[str]) -> tuple[str, ...]:
    """Return model names in first-seen order, rejecting an empty sequence."""
    if isinstance(models, str):
        raise TypeError("models must be an iterable of model names, not a single string")
    ordered = tuple(dict.fromkeys(models))
    if not ordered:
        raise ValueError("at least one Codex model is required")
    return ordered
