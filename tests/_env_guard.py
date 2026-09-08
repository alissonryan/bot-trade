"""Refuse to run the suite under an interpreter that lacks this project's deps.

The failure this prevents is not a crash, it is a *misreading*. Running
`python -m pytest` with the system interpreter leaves `websockets` and
`playwright` missing, and the chart-server tests then fail with an import error
that looks exactly like a pre-existing gap in the project. A whole session of
agent-written reports repeated "2 pre-existing failures" on that basis before
anyone ran the suite under .venv and found it green.

So the guard's job is to be unmissable and to name the interpreter, rather than
to let a wrong environment express itself as a plausible-looking test failure.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

# A requirement line is `name`, optionally followed by extras, a version
# specifier, or a marker. We only want the distribution name.
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def required_distributions(requirements: Path) -> list[str]:
    """Distribution names declared in a requirements file, in order."""
    names = []
    for line in requirements.read_text().splitlines():
        line = line.split("#", 1)[0]
        if not line.strip() or line.lstrip().startswith("-"):
            continue
        match = _NAME.match(line)
        if match:
            names.append(match.group(1))
    return names


def missing_distributions(names: list[str]) -> list[str]:
    """Which of ``names`` are not installed for the running interpreter.

    Distribution names, not import names: this is what makes `python-dotenv`
    work without a hand-maintained map to `dotenv`.
    """
    missing = []
    for name in names:
        try:
            version(name)
        except PackageNotFoundError:
            missing.append(name)
    return missing
