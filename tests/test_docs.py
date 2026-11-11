"""The English and German documents must keep the same structure."""

import re
from pathlib import Path

import pytest

from osa_tools.playbooks import REPOSITORY_ROOT

_HEADING = re.compile(r"^(#{1,3}) (?:(\d+(?:\.\d+)?)\.? )?", re.MULTILINE)
_LINK = re.compile(r"\]\(([^)#]+)(?:#[^)]*)?\)")
_NUMBERED_HEADING = re.compile(r"^#{2,3} (\d+(?:\.\d+)?)\.? ")
_LIST_ITEM = re.compile(r"^\s*(?:[-*]|\d+\.) ")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,:]\d+)*(?![\w])")
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


def _section_fingerprints(name: str) -> dict[str, tuple[int, int, tuple[str, ...]]]:
    """Per numbered section: list items, table rows and the numbers in the prose
    (headings, code blocks, inline code and link targets left out). A bullet or
    a figure that exists in one language only changes the fingerprint."""
    sections: dict[str, list[str]] = {"0": []}
    section = "0"
    in_code = False
    text = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        heading = _NUMBERED_HEADING.match(line)
        if heading:
            section = heading.group(1)
            sections.setdefault(section, [])
            continue
        sections[section].append(line)
    fingerprints: dict[str, tuple[int, int, tuple[str, ...]]] = {}
    for key, lines in sections.items():
        prose = re.sub(r"`[^`]*`|\]\([^)]*\)", "", "\n".join(lines))
        fingerprints[key] = (
            sum(bool(_LIST_ITEM.match(line)) for line in lines),
            sum(line.startswith("|") for line in lines),
            tuple(sorted(_NUMBER.findall(prose))),
        )
    return fingerprints


def test_readme_sections_keep_the_same_items_rows_and_numbers() -> None:
    assert _section_fingerprints("README.md") == _section_fingerprints("README.de.md")
