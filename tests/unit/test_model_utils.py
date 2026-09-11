from typing import cast

import pytest

from codex_review.model_utils import (
    ReasoningEffort,
    dedupe_models,
    effective_reasoning_effort,
)


def test_dedupe_models_preserves_first_seen_order() -> None:
    assert dedupe_models(("gpt-5.6-sol", "gpt-5.5", "gpt-5.6-sol")) == (
        "gpt-5.6-sol",
        "gpt-5.5",
    )


def test_dedupe_models_rejects_empty_sequence() -> None:
    with pytest.raises(ValueError, match="at least one Codex model is required"):
        dedupe_models(())


def test_dedupe_models_rejects_single_string() -> None:
    with pytest.raises(TypeError, match="not a single string"):
        dedupe_models("gpt-5.6-sol")


def test_effective_reasoning_effort_downgrades_only_unsupported_models() -> None:
    assert effective_reasoning_effort("gpt-6-astra", "ultra") == "ultra"
    assert effective_reasoning_effort("gpt-reserve", "max") == "max"
    assert effective_reasoning_effort("gpt-5.6-luna", "max") == "max"
    assert effective_reasoning_effort("gpt-5.3-codex-spark", "max") == "xhigh"
    assert effective_reasoning_effort("gpt-5.3-codex-spark", "ultra") == "xhigh"
    assert effective_reasoning_effort("custom-review-model", "ultra") == "ultra"


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
def test_effective_reasoning_effort_preserves_supported_values(effort: str) -> None:
    requested = cast(ReasoningEffort, effort)
    assert effective_reasoning_effort("gpt-5.3-codex-spark", requested) == effort
