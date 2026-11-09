"""Thin, shell-free wrappers around subprocess."""

import subprocess
from collections.abc import Sequence
from pathlib import Path

from osa_tools.errors import OsaError

_STDERR_TAIL_LINES = 5


def run(argv: Sequence[str], cwd: Path | None = None) -> int:
    """Run a command attached to the terminal and return its exit code."""
    try:
        return subprocess.run(argv, cwd=cwd, check=False).returncode  # noqa: S603
    except FileNotFoundError as exc:
        msg = f"'{argv[0]}' is not installed or not on PATH."
        raise OsaError(msg) from exc


def capture(argv: Sequence[str]) -> str:
    """Run a command, return its stdout and raise OsaError when it fails."""
    try:
        result = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        msg = f"'{argv[0]}' is not installed or not on PATH."
        raise OsaError(msg) from exc
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-_STDERR_TAIL_LINES:])
        msg = f"'{argv[0]}' failed (exit {result.returncode}): {tail}"
        raise OsaError(msg)
    return result.stdout
