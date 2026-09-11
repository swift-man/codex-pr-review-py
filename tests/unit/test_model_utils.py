import pytest

from codex_review.model_utils import (
    dedupe_models,
    effective_reasoning_effort,
    incompatible_reasoning_effort_models,
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


def test_incompatible_reasoning_effort_models_reports_known_fallbacks() -> None:
    assert incompatible_reasoning_effort_models(
        ("gpt-5.6-sol", "gpt-5.5", "gpt-5.3-codex-spark"),
        "max",
    ) == ("gpt-5.5", "gpt-5.3-codex-spark")


def test_incompatible_reasoning_effort_models_allows_unknown_models() -> None:
    assert incompatible_reasoning_effort_models(("custom-review-model",), "ultra") == ()


def test_incompatible_reasoning_effort_models_checks_known_aliases() -> None:
    assert incompatible_reasoning_effort_models(
        ("gpt-reserve", "codex-auto-review"),
        "ultra",
    ) == ("gpt-reserve", "codex-auto-review")


def test_effective_reasoning_effort_downgrades_only_unsupported_models() -> None:
    assert effective_reasoning_effort("gpt-reserve", "max") == "max"
    assert effective_reasoning_effort("gpt-5.6-luna", "max") == "max"
    assert effective_reasoning_effort("gpt-5.3-codex-spark", "max") == "xhigh"
    assert effective_reasoning_effort("gpt-5.3-codex-spark", "ultra") == "xhigh"
