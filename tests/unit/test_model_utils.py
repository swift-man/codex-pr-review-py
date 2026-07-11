import pytest

from codex_review.model_utils import dedupe_models


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
