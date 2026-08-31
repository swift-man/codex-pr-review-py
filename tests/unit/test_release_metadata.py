import re
import tomllib
from pathlib import Path

from codex_review import __version__

_ROOT = Path(__file__).resolve().parents[2]


def test_version_txt_matches_project_and_package_versions() -> None:
    version = (_ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert version == project["project"]["version"]
    assert version == __version__


def test_changelog_contains_current_version() -> None:
    version = (_ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert f"## [{version}]" in changelog


def test_pydantic_minimum_supports_data_aware_default_factory() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "pydantic>=2.10" in project["project"]["dependencies"]
