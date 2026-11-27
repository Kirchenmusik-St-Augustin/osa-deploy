"""Shared helpers for the wrapper tests."""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from osa_tools.site import Site

# deploy.yml copies quadlets/augustiner-stats/ to the host as it is, so the
# tests must not leave compiled files of the statistics package in it: they
# would be copied along and report a change on every deploy.
sys.dont_write_bytecode = True

PASSWORD = "correct-horse-battery-staple-1"


def make_reader(*answers: str) -> Callable[[str], str]:
    """Return a password reader that hands out the answers in order."""
    queue = list(answers)

    def read(question: str) -> str:
        assert queue, f"unexpected question: {question}"
        return queue.pop(0)

    return read


@pytest.fixture
def site(tmp_path: Path) -> Site:
    """A stage directory with the files load_site() requires."""
    directory = tmp_path / "sites" / "test"
    (directory / "secrets").mkdir(parents=True)
    (directory / "inventory.ini").write_text(
        "[vps]\nhost.example.org\n\n[vps:vars]\napp_environment=test\n",
        encoding="utf-8",
    )
    (directory / "operator_key.vault").write_text("placeholder", encoding="utf-8")
    return Site(name="test", directory=directory)
