from collections.abc import Iterable


def dedupe_models(models: Iterable[str]) -> tuple[str, ...]:
    """Return model names in first-seen order, rejecting an empty sequence."""
    seen: set[str] = set()
    ordered: list[str] = []
    for model in models:
        if model in seen:
            continue
        seen.add(model)
        ordered.append(model)
    if not ordered:
        raise ValueError("at least one Codex model is required")
    return tuple(ordered)
