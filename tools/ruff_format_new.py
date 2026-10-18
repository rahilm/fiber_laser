"""pre-commit hook: ``ruff format`` only the Python files this commit adds.

The existing code keeps its hand-aligned layout (no mass reformat); new files follow
``ruff format`` from the start.  Usage (from .pre-commit-config.yaml): the hook receives the
staged file names, keeps those git reports as added (``--diff-filter=A``) and formats them;
pre-commit then fails the commit if a file changed, so the result can be reviewed and staged.
"""

from __future__ import annotations

import subprocess
import sys


def added_files() -> set[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=A", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {f for f in out.split("\0") if f}


def main(argv: list[str]) -> int:
    new = sorted(set(argv) & added_files())
    if not new:
        return 0
    return subprocess.run(["ruff", "format", *new]).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
