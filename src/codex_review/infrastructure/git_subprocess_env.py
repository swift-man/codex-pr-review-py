import os
from collections.abc import Mapping


def git_subprocess_env(extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a non-interactive git environment for review worker subprocesses."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    return env
