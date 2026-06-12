from codex_review.infrastructure.git_subprocess_env import git_subprocess_env


def test_git_subprocess_env_disables_interactive_prompts(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EXISTING_ENV", "kept")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setenv("LC_ALL", "ko_KR.UTF-8")

    env = git_subprocess_env()

    assert env["EXISTING_ENV"] == "kept"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["LC_ALL"] == "C"


def test_git_subprocess_env_preserves_extra_auth_config(
    monkeypatch,
) -> None:
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)

    env = git_subprocess_env({"GIT_CONFIG_COUNT": "1"})

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
