"""The English and German documents must keep the same structure."""

import re
from pathlib import Path

import pytest

from osa_tools.playbooks import REPOSITORY_ROOT

_HEADING = re.compile(r"^(#{1,3}) (?:(\d+(?:\.\d+)?)\.? )?", re.MULTILINE)
_LINK = re.compile(r"\]\(([^)#]+)(?:#[^)]*)?\)")
_PAIRS = [("README.md", "README.de.md"), ("HANDOVER.md", "HANDOVER.de.md")]


def _outline(name: str) -> list[tuple[str, str]]:
    text = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
    return [(level, number) for level, number in _HEADING.findall(text)]


@pytest.mark.parametrize(("english", "german"), _PAIRS)
def test_both_languages_have_the_same_headings(english: str, german: str) -> None:
    assert _outline(english) == _outline(german)


@pytest.mark.parametrize("name", [english for english, _ in _PAIRS])
def test_the_outline_is_not_empty(name: str) -> None:
    assert len(_outline(name)) >= 2


@pytest.mark.parametrize("name", [name for pair in _PAIRS for name in pair])
def test_relative_links_point_at_existing_files(name: str) -> None:
    text = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
    relative = [
        target
        for target in _LINK.findall(text)
        if not target.startswith(("http://", "https://", "mailto:"))
    ]
    missing = [t for t in relative if not (Path(REPOSITORY_ROOT) / t).exists()]
    assert missing == []
