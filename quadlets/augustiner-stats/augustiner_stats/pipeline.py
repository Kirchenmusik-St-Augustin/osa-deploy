"""One hourly run: read the raw log, close past days, render the dashboard.

Data directory layout:
  state/   month secrets and the queue of closed days for GoAccess
  days/    per closed day: summary (JSON) and the two GoAccess line files
  current/ the same for the day in progress, rewritten every run
  geo/     the address tables and databases of the geodata job
"""

import gzip
import logging
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from augustiner_stats.accesslog import Request, read_unfinished
from augustiner_stats.classify import classify_client, group_by_client
from augustiner_stats.dashboard import Site, Tab, render_archive, render_period
from augustiner_stats.days import (
    UNKNOWN_COUNTRY,
    DaySummary,
    Person,
    Program,
    goaccess_line,
    read_summary,
    summarize,
    write_summary,
)
from augustiner_stats.ipranges import AddressLookup
from augustiner_stats.pages import page_titles
from augustiner_stats.periods import build_dashboard
from augustiner_stats.pseudonym import (
    MonthSecrets,
    month_of,
    pseudonymous_address,
    visitor_id,
)
from augustiner_stats.useragent import parse_agent, reduced_header

LOG = logging.getLogger(__name__)
PEOPLE_DAYS = 25 * 31
PROGRAM_DAYS = 90
PEOPLE = "menschen"
PROGRAMS = "automatisch"
QUEUE = "goaccess-queue"
SITE_TITLE = "Besucherstatistik augustinerkirche.at"
SITE_NAME = "Augustinerkirche Wien"
NO_USER = "-"


class NotEnoughSpaceError(RuntimeError):
    """The disk is too full to write more statistics safely."""


@dataclass(frozen=True)
class Settings:
    logs: Path
    data: Path
    www: Path
    site: Path
    own_hosts: frozenset[str]
    zone: ZoneInfo
    min_free_mb: int


@dataclass(frozen=True)
class Geodata:
    """The address tables of the geodata job; missing ones answer nothing."""

    countries: AddressLookup
    datacenters: AddressLookup

    @classmethod
    def load(cls, directory: Path) -> "Geodata":
        return cls(
            countries=AddressLookup(
                directory / "country-v4.bin", directory / "country-v6.bin"
            ),
            datacenters=AddressLookup(
                directory / "datacenter-v4.bin", directory / "datacenter-v6.bin"
            ),
        )

    def country(self, ip: str) -> str:
        return self.countries.lookup(ip) or UNKNOWN_COUNTRY

    def is_datacenter(self, ip: str) -> bool:
        return self.datacenters.lookup(ip) is not None


@dataclass
class DayResult:
    summary: DaySummary
    people_lines: list[str]
    program_lines: list[str]


def _in_time_order(lines: Iterable[tuple[datetime, str]]) -> list[str]:
    return [line for _, line in sorted(lines, key=lambda item: item[0])]


def process_day(
    day: date,
    requests: Sequence[Request],
    secret: bytes,
    geodata: Geodata,
    own_hosts: frozenset[str],
) -> DayResult:
    """Classifies the day's clients and turns them into summary and lines."""
    people: list[Person] = []
    programs: list[Program] = []
    people_lines: list[tuple[datetime, str]] = []
    program_lines: list[tuple[datetime, str]] = []
    for (ip, agent), client in group_by_client(requests).items():
        address = pseudonymous_address(ip, secret)
        category = classify_client(client, geodata.is_datacenter)
        if category is None:
            header = reduced_header(parse_agent(agent))
            people.append(
                Person(visitor_id(ip, agent, secret), geodata.country(ip), client)
            )
            people_lines += [
                (r.time, goaccess_line(r, address, NO_USER, header)) for r in client
            ]
            continue
        programs.append(Program(category.value, len(client)))
        program_lines += [
            (r.time, goaccess_line(r, address, category.token, agent)) for r in client
        ]
    return DayResult(
        summary=summarize(day, people, programs, own_hosts),
        people_lines=_in_time_order(people_lines),
        program_lines=_in_time_order(program_lines),
    )


# --- files ---------------------------------------------------------------------


def _write_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _write_lines(path: Path, lines: Sequence[str]) -> None:
    text = "".join(f"{line}\n" for line in lines)
    if path.suffix != ".gz":
        _write_text(path, text)
        return
    temporary = path.with_name(path.name + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as stream:
        stream.write(text)
    temporary.replace(path)


@dataclass(frozen=True)
class Layout:
    """Where the run keeps what, below the data directory."""

    root: Path

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def days(self) -> Path:
        return self.root / "days"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def geo(self) -> Path:
        return self.root / "geo"

    def create(self) -> None:
        for directory in (self.state, self.days, self.current, self.geo):
            directory.mkdir(parents=True, exist_ok=True)

    def finished_days(self) -> set[date]:
        return {
            date.fromisoformat(path.stem) for path in self.days.glob("????-??-??.json")
        }

    def store_day(self, result: DayResult) -> None:
        day = result.summary.day.isoformat()
        _write_lines(self.days / f"{day}.{PEOPLE}.log.gz", result.people_lines)
        _write_lines(self.days / f"{day}.{PROGRAMS}.log.gz", result.program_lines)
        write_summary(self.days / f"{day}.json", result.summary)
        with (self.state / QUEUE).open("a", encoding="utf-8") as queue:
            queue.write(f"{day}\n")

    def store_current(self, result: DayResult | None) -> None:
        _write_lines(
            self.current / f"{PEOPLE}.log", result.people_lines if result else []
        )
        _write_lines(
            self.current / f"{PROGRAMS}.log", result.program_lines if result else []
        )
        today = self.current / "today.json"
        if result is None:
            today.unlink(missing_ok=True)
            return
        write_summary(today, result.summary)

    def summaries(self) -> dict[date, DaySummary]:
        store = {
            summary.day: summary
            for summary in map(read_summary, sorted(self.days.glob("????-??-??.json")))
        }
        today = self.current / "today.json"
        if today.is_file():
            summary = read_summary(today)
            store[summary.day] = summary
        return store

    def remove_expired(self, today: date) -> None:
        """People's files after 25 months, programs' line files after 90 days."""
        for path in self.days.iterdir():
            day = date.fromisoformat(path.name[:10])
            age = (today - day).days
            limit = PROGRAM_DAYS if f".{PROGRAMS}." in path.name else PEOPLE_DAYS
            if age > limit:
                path.unlink()


def _require_space(directory: Path, min_free_mb: int) -> None:
    free_mb = shutil.disk_usage(directory).free // (1024 * 1024)
    if free_mb < min_free_mb:
        msg = (
            f"only {free_mb} MB free below {directory}, "
            f"at least {min_free_mb} MB needed"
        )
        raise NotEnoughSpaceError(msg)


def _forget_closed_months(secrets: MonthSecrets, today: date) -> None:
    """Every day before today is closed after a run, so older months are done."""
    current = month_of(today)
    for month in secrets.months():
        if month < current:
            secrets.forget(month)
            LOG.info("forgot the secret of %s", month)


# --- pages ---------------------------------------------------------------------


def write_pages(
    www: Path,
    store: Mapping[date, DaySummary],
    now: datetime,
    titles: Mapping[str, str],
    has_geodata: bool,  # noqa: FBT001 - plain data flag, passed by keyword below
) -> None:
    dashboard = build_dashboard(store, now, titles)
    site = Site(SITE_TITLE, dashboard.tabs, now, preview=False, has_geodata=has_geodata)
    archive = www / "archiv"
    archive.mkdir(parents=True, exist_ok=True)
    for report in dashboard.reports:
        _write_text(www / f"{report.slug}.html", render_period(site, report))
    months: list[Tab] = []
    for report in dashboard.archive:
        _write_text(
            archive / f"{report.slug}.html", render_period(site, report, archived=True)
        )
        months.append(Tab(report.slug, report.heading))
    _write_text(www / "archiv.html", render_archive(site, months))


def run(
    settings: Settings, now: datetime, geodata_loader: Callable[[Path], Geodata]
) -> None:
    layout = Layout(settings.data)
    layout.create()
    _require_space(settings.data, settings.min_free_mb)
    today = now.date()
    reading = read_unfinished(settings.logs, layout.finished_days(), settings.zone)
    geodata = geodata_loader(layout.geo)
    secrets = MonthSecrets(layout.state)
    current: DayResult | None = None
    for day in sorted(reading.days):
        if day > today:
            continue
        result = process_day(
            day, reading.days[day], secrets.for_day(day), geodata, settings.own_hosts
        )
        if day == today:
            current = result
            continue
        layout.store_day(result)
        LOG.info("closed %s", day)
    layout.store_current(current)
    _forget_closed_months(secrets, today)
    layout.remove_expired(today)
    titles = page_titles(settings.site, SITE_NAME)
    has_geodata = geodata.countries.available
    write_pages(settings.www, layout.summaries(), now, titles, has_geodata=has_geodata)
    geodata.countries.close()
    geodata.datacenters.close()
