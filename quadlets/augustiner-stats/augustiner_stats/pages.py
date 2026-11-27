"""Which requests are page views, in which language, under which title."""

import html
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

LANGUAGES = {
    "en": "Englisch",
    "it": "Italienisch",
    "fr": "Französisch",
    "es": "Spanisch",
}
GERMAN = "Deutsch"
HOME = "/"
HOME_TITLE = "Startseite"
VIEW_STATUSES = frozenset({200, 304})
# Served by the service worker only when the visitor is offline.
NOT_A_VISIT = frozenset({"/offline.html"})
_LANGUAGE_PREFIX = re.compile(r"^/(en|it|fr|es)(?=/|$)")
_TITLE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
TITLE_SEPARATOR = " — "


class Viewable(Protocol):
    @property
    def method(self) -> str: ...
    @property
    def path(self) -> str: ...
    @property
    def status(self) -> int: ...


def is_page_view(request: Viewable) -> bool:
    """A successful GET of an HTML page (304 too: the browser showed it again)."""
    if request.method != "GET" or request.status not in VIEW_STATUSES:
        return False
    path = request.path
    if path in NOT_A_VISIT:
        return False
    return path.endswith((".html", "/")) or _LANGUAGE_PREFIX.fullmatch(path) is not None


def language(path: str) -> str:
    match = _LANGUAGE_PREFIX.match(path)
    return LANGUAGES[match.group(1)] if match else GERMAN


def page_of(path: str) -> str:
    """The same page in every language: /en/pages/musik.html -> /pages/musik.html."""
    page = _LANGUAGE_PREFIX.sub("", path) or HOME
    if page.endswith("/index.html"):
        page = page.removesuffix("index.html")
    return page if page.startswith("/") else f"/{page}"


def _name_from_file(path: str) -> str:
    stem = Path(path).stem.replace("-", " ")
    return stem[:1].upper() + stem[1:]


def _title_of(document: Path, site_name: str) -> str | None:
    match = _TITLE.search(document.read_text(encoding="utf-8", errors="replace"))
    if not match:
        return None
    title = html.unescape(match.group(1)).strip()
    head = title.split(TITLE_SEPARATOR, 1)[0].strip()
    return None if not head or head == site_name else head


def page_titles(site: Path, site_name: str) -> dict[str, str]:
    """German titles of the pages; a page without a title of its own is named
    after its file."""
    titles = {HOME: HOME_TITLE}
    pages = site / "pages"
    if not pages.is_dir():
        return titles
    for document in sorted(pages.rglob("*.html")):
        path = "/" + document.relative_to(site).as_posix()
        titles[path] = _title_of(document, site_name) or _name_from_file(path)
    return titles


def title_for(path: str, titles: Mapping[str, str]) -> str:
    return titles.get(path) or _name_from_file(path) or path
