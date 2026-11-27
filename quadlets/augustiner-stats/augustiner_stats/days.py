"""One day of visits: the summary the dashboard adds up, and the GoAccess lines."""

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit

from augustiner_stats.accesslog import Request
from augustiner_stats.pages import is_page_view, language, page_of
from augustiner_stats.useragent import parse_agent

VISIT_GAP = timedelta(minutes=30)
REDIRECT_STATUSES = frozenset({301, 302, 307, 308})
UNKNOWN_COUNTRY = "??"

DIRECT = "Direkt"
SEARCH = "Suchmaschinen"
SOCIAL = "Soziale Medien"
CAMPAIGN = "Newsletter & Kampagnen"
OTHER_SITES = "Andere Websites"
SEARCH_HOSTS = re.compile(
    r"(?:^|\.)(?:google|bing|duckduckgo|ecosia|yahoo|qwant|startpage|yandex|baidu"
    r"|search\.brave|brave|mojeek|metager|ask|aol|seznam|perplexity)\.[a-z.]+$"
)
SOCIAL_HOSTS = re.compile(
    r"(?:^|\.)(?:facebook|fb|instagram|twitter|x|t|linkedin|lnkd|youtube|pinterest"
    r"|threads|reddit|tiktok|mastodon|bsky|xing|whatsapp)\.[a-z.]+$"
)


@dataclass
class DaySummary:
    """Totals of the people on one day; programs only as counts per category."""

    day: date
    # Visitor id -> [device, browser, country]: one entry per visitor, so a
    # period can count each visitor once per calendar month everywhere.
    profiles: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    hourly: list[int] = field(default_factory=lambda: [0] * 24)
    page_views: int = 0
    visits: int = 0
    single_page_visits: int = 0
    timed_visits: int = 0
    timed_seconds: int = 0
    sources: dict[str, int] = field(default_factory=dict[str, int])
    entries: dict[str, int] = field(default_factory=dict[str, int])
    languages: dict[str, int] = field(default_factory=dict[str, int])
    pages: dict[str, int] = field(default_factory=dict[str, int])
    # Per category: how many programs came (each client once a day, however
    # much it asked) and how many requests they made.
    programs: dict[str, int] = field(default_factory=dict[str, int])
    program_requests: dict[str, int] = field(default_factory=dict[str, int])

    @property
    def visitors(self) -> list[str]:
        return sorted(self.profiles)


def source_of(view: Request, own_hosts: frozenset[str]) -> str:
    """Where the first page of a visit was reached from."""
    if any(key.startswith("utm_") for key, _ in parse_qsl(view.query)):
        return CAMPAIGN
    host = (urlsplit(view.referer).hostname or "").lower()
    if not host or host in own_hosts:
        return DIRECT
    if SEARCH_HOSTS.search(host):
        return SEARCH
    if SOCIAL_HOSTS.search(host):
        return SOCIAL
    return OTHER_SITES


def _visits(events: Sequence[Request]) -> list[list[Request]]:
    visits: list[list[Request]] = []
    for event in events:
        if visits and event.time - visits[-1][-1].time <= VISIT_GAP:
            visits[-1].append(event)
        else:
            visits.append([event])
    return visits


@dataclass
class _Tally:
    profiles: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    hourly: defaultdict[int, set[str]] = field(
        default_factory=lambda: defaultdict[int, set[str]](set)
    )
    counters: defaultdict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict[str, Counter[str]](Counter)
    )
    numbers: Counter[str] = field(default_factory=Counter[str])


def _count_visit(
    visit: Sequence[Request], tally: _Tally, own_hosts: frozenset[str]
) -> None:
    views = [event for event in visit if is_page_view(event)]
    if not views:
        return
    tally.numbers["visits"] += 1
    tally.counters["entries"][visit[0].host] += 1
    tally.counters["sources"][source_of(views[0], own_hosts)] += 1
    if len(views) == 1:
        tally.numbers["single_page_visits"] += 1
        return
    tally.numbers["timed_visits"] += 1
    tally.numbers["timed_seconds"] += round(
        (views[-1].time - views[0].time).total_seconds()
    )


def _count_client(
    visitor: str,
    requests: Sequence[Request],
    country: str,
    tally: _Tally,
    own_hosts: frozenset[str],
) -> None:
    views = [request for request in requests if is_page_view(request)]
    if not views:
        return
    agent = parse_agent(requests[0].user_agent)
    tally.profiles[visitor] = [agent.device, agent.browser, country]
    for view in views:
        tally.hourly[view.time.hour].add(visitor)
        tally.counters["languages"][language(view.path)] += 1
        tally.counters["pages"][page_of(view.path)] += 1
    tally.numbers["page_views"] += len(views)
    events = [
        request
        for request in requests
        if is_page_view(request)
        or (request.method == "GET" and request.status in REDIRECT_STATUSES)
    ]
    for visit in _visits(events):
        _count_visit(visit, tally, own_hosts)


@dataclass(frozen=True)
class Person:
    """One client the classifier took for a person, with its pseudonymous id."""

    visitor: str
    country: str
    requests: Sequence[Request]


@dataclass(frozen=True)
class Program:
    """One client the classifier took for a program: its category and requests."""

    category: str
    requests: int


def summarize(
    day: date,
    people: Iterable[Person],
    programs: Iterable[Program],
    own_hosts: frozenset[str],
) -> DaySummary:
    tally = _Tally()
    for person in people:
        _count_client(person.visitor, person.requests, person.country, tally, own_hosts)
    program_counts: Counter[str] = Counter()
    program_requests: Counter[str] = Counter()
    for program in programs:
        program_counts[program.category] += 1
        program_requests[program.category] += program.requests
    numbers = tally.numbers
    return DaySummary(
        day=day,
        profiles=dict(sorted(tally.profiles.items())),
        hourly=[len(tally.hourly[hour]) for hour in range(24)],
        page_views=numbers["page_views"],
        visits=numbers["visits"],
        single_page_visits=numbers["single_page_visits"],
        timed_visits=numbers["timed_visits"],
        timed_seconds=numbers["timed_seconds"],
        sources=dict(tally.counters["sources"]),
        entries=dict(tally.counters["entries"]),
        languages=dict(tally.counters["languages"]),
        pages=dict(tally.counters["pages"]),
        programs=dict(program_counts),
        program_requests=dict(program_requests),
    )


# --- storage -----------------------------------------------------------------

_COUNTERS = (
    "sources",
    "entries",
    "languages",
    "pages",
    "programs",
    "program_requests",
)
_NUMBERS = (
    "page_views",
    "visits",
    "single_page_visits",
    "timed_visits",
    "timed_seconds",
)


def summary_json(summary: DaySummary) -> str:
    data = asdict(summary)
    data["day"] = summary.day.isoformat()
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


PROFILE_FIELDS = 3


def _profiles(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        msg = "profiles is not a mapping"
        raise TypeError(msg)
    items = cast("dict[object, object]", value).items()
    for visitor, profile in items:
        if (
            not isinstance(visitor, str)
            or not isinstance(profile, list)
            or len(cast("list[object]", profile)) != PROFILE_FIELDS
            or not all(isinstance(part, str) for part in cast("list[object]", profile))
        ):
            msg = "profiles must map visitor ids to [device, browser, country]"
            raise TypeError(msg)
    return cast("dict[str, list[str]]", value)


def _count_map(value: object, name: str) -> dict[str, int]:
    if not isinstance(value, dict):
        msg = f"{name} is not a mapping"
        raise TypeError(msg)
    items = cast("dict[object, object]", value).items()
    if not all(isinstance(k, str) and type(v) is int and v >= 0 for k, v in items):
        msg = f"{name} must map text to counts"
        raise TypeError(msg)
    return cast("dict[str, int]", value)


def _count(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        msg = f"{name} is not a count"
        raise TypeError(msg)
    return value


def _text_list(value: object, name: str, length: int | None = None) -> list[object]:
    if not isinstance(value, list):
        msg = f"{name} is not a list"
        raise TypeError(msg)
    items = cast("list[object]", value)
    if length is not None and len(items) != length:
        msg = f"{name} must have {length} entries"
        raise TypeError(msg)
    return items


def summary_from_json(text: str) -> DaySummary:
    """Reads a stored summary and refuses anything not shaped like one."""
    loaded: object = json.loads(text)
    if not isinstance(loaded, dict):
        msg = "a day summary is a JSON object"
        raise TypeError(msg)
    data = cast("dict[str, object]", loaded)
    day = data.get("day")
    if not isinstance(day, str):
        msg = "day is missing"
        raise TypeError(msg)
    hourly = [
        _count(value, "hourly")
        for value in _text_list(data.get("hourly"), "hourly", 24)
    ]
    summary = DaySummary(
        day=date.fromisoformat(day),
        profiles=_profiles(data.get("profiles")),
        hourly=hourly,
    )
    for name in _NUMBERS:
        setattr(summary, name, _count(data.get(name), name))
    for name in _COUNTERS:
        setattr(summary, name, _count_map(data.get(name), name))
    return summary


def write_summary(path: Path, summary: DaySummary) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(summary_json(summary), encoding="utf-8")
    temporary.replace(path)


def read_summary(path: Path) -> DaySummary:
    return summary_from_json(path.read_text(encoding="utf-8"))


# --- GoAccess lines ------------------------------------------------------------

# Read by GoAccess with this format (report.sh); one request per line.
GOACCESS_FORMAT = '%v %h %e [%d:%t] "%r" %s %b "%R" "%u" %D'
_UNSAFE = re.compile(r'["\\\x00-\x1f\x7f]')
_SPACE = re.compile(r"\s")
MAX_HEADER = 400


def _clean(text: str) -> str:
    return _UNSAFE.sub("", text)[:MAX_HEADER] or "-"


def kept_query(query: str) -> str:
    """Only campaign parameters survive; anything else might identify someone."""
    pairs = [(key, value) for key, value in parse_qsl(query) if key.startswith("utm_")]
    return urlencode(pairs)


def _referer(referer: str) -> str:
    parts = urlsplit(referer)
    if not parts.scheme or not parts.netloc:
        return "-"
    return _clean(f"{parts.scheme}://{parts.netloc}{parts.path}")


def goaccess_line(request: Request, address: str, user: str, agent: str) -> str:
    path = _SPACE.sub("%20", _clean(request.path))
    query = kept_query(request.query)
    target = f"{path}?{query}" if query else path
    protocol = _clean(request.protocol) if request.protocol else "HTTP/1.1"
    return (
        f"{request.host} {address} {user} [{request.time:%Y-%m-%d:%H:%M:%S}] "
        f'"{_clean(request.method)} {target} {protocol}" '
        f"{request.status} {request.size} "
        f'"{_referer(request.referer)}" "{_clean(agent)}" '
        f"{round(request.duration * 1e6)}"
    )
