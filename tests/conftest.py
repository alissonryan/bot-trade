import sys
from pathlib import Path

import pytest

from tests._env_guard import missing_distributions, required_distributions

_ROOT = Path(__file__).resolve().parent.parent


def pytest_configure(config: pytest.Config) -> None:
    requirements = _ROOT / "requirements.txt"
    if not requirements.exists():  # nothing to check against
        return
    missing = missing_distributions(required_distributions(requirements))
    if not missing:
        return
    # Exit rather than fail: a wrong interpreter invalidates the whole run, and
    # a run that reports "2 failed" invites someone to explain the failures
    # instead of fixing the environment.
    pytest.exit(
        "wrong interpreter: "
        f"{sys.executable}\n"
        f"missing from requirements.txt: {', '.join(missing)}\n\n"
        "Run the suite with ./scripts/test, which finds this project's "
        "virtualenv (including from a git worktree, which has no .venv of "
        "its own).\n"
        "Any result produced under this interpreter is not evidence about "
        "the project.",
        returncode=pytest.ExitCode.USAGE_ERROR,
    )
