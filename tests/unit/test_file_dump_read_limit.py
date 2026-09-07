from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest

from codex_review.domain import ReviewPathFilter, TokenBudget
from codex_review.infrastructure import file_dump_collector as collector


@pytest.mark.parametrize("path,always_review", [
    ("package.json", ()), ("Package.swift", ()),
    ("forced.txt", ("forced.txt",)), ("source.py", ()),
])
@pytest.mark.parametrize("size", [64, 65])
def test_absolute_read_limit_preserves_diff_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    path: str, always_review: tuple[str, ...], size: int,
) -> None:
    monkeypatch.setattr(collector, "_MAX_FILE_READ_BYTES", 64, raising=False)
    (tmp_path / path).write_text("x" * size)
    dump = collector._build_dump_sync(
        tmp_path, [path], (path,), {path}, TokenBudget(max_tokens=10_000),
        1_000, 1_000, ReviewPathFilter(always_review=always_review),
    )
    assert not dump.filter_excluded
    if size > 64:
        assert not dump.entries
        assert dump.budget_trimmed == (path,)
        assert dump.exceeded_budget
    else:
        assert dump.entries[0].content == "x" * size
        assert not dump.exceeded_budget


def test_read_is_bounded_even_when_file_grows_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector, "_MAX_FILE_READ_BYTES", 64)
    path = tmp_path / "package.json"
    path.write_bytes(b"x")
    source = Mock()
    source.read.return_value = b"x" * 65
    opened = Mock(return_value=nullcontext(source))
    monkeypatch.setattr(Path, "open", opened)
    dump = collector._build_dump_sync(
        tmp_path, [path.name], (path.name,), {path.name},
        TokenBudget(max_tokens=10_000), 1, 1, ReviewPathFilter(),
    )
    opened.assert_called_once_with("rb")
    source.read.assert_called_once_with(65)
    assert dump.budget_trimmed == (path.name,)
    assert not dump.filter_excluded


def test_limit_is_in_bytes_before_utf8_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector, "_MAX_FILE_READ_BYTES", 64, raising=False)
    (tmp_path / "package.json").write_bytes(b"\xe2\x82\xac" * 22)
    dump = collector._build_dump_sync(
        tmp_path, ["package.json"], ("package.json",), {"package.json"},
        TokenBudget(max_tokens=10_000), 1, 1, ReviewPathFilter(),
    )
    assert dump.budget_trimmed == ("package.json",)
    assert not dump.filter_excluded


def test_bounded_read_preserves_universal_newlines(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_bytes(b"a\r\nb\rc\n")
    dump = collector._build_dump_sync(
        tmp_path, ["source.py"], ("source.py",), {"source.py"},
        TokenBudget(max_tokens=10_000), 1_000, 1_000, ReviewPathFilter(),
    )
    assert dump.entries[0].content == "a\nb\nc\n"
