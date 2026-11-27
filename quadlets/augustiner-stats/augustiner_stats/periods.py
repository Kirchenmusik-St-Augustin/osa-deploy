"""Add day summaries up to the periods the dashboard shows."""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from augustiner_stats.dashboard import (
    WEEKDAYS,
    DomainEntry,
    Kpi,
    PeriodReport,
    Point,
    Share,
    Tab,
)
from augustiner_stats.days import UNKNOWN_COUNTRY, DaySummary
from augustiner_stats.pages import title_for
from augustiner_stats.pseudonym import month_of

MONTHS = (
    "Jänner",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)
MONTHS_SHORT = (
    "Jän.",
    "Feb.",
    "März",
    "Apr.",
    "Mai",
    "Juni",
    "Juli",
    "Aug.",
    "Sep.",
    "Okt.",
    "Nov.",
    "Dez.",
)
COUNTRIES = {
    "AT": "Österreich",
    "DE": "Deutschland",
    "CH": "Schweiz",
    "IT": "Italien",
    "FR": "Frankreich",
    "ES": "Spanien",
    "PT": "Portugal",
    "NL": "Niederlande",
    "BE": "Belgien",
    "LU": "Luxemburg",
    "LI": "Liechtenstein",
    "GB": "Vereinigtes Königreich",
    "IE": "Irland",
    "DK": "Dänemark",
    "SE": "Schweden",
    "NO": "Norwegen",
    "FI": "Finnland",
    "PL": "Polen",
    "CZ": "Tschechien",
    "SK": "Slowakei",
    "HU": "Ungarn",
    "SI": "Slowenien",
    "HR": "Kroatien",
    "RS": "Serbien",
    "BA": "Bosnien und Herzegowina",
    "RO": "Rumänien",
    "BG": "Bulgarien",
    "GR": "Griechenland",
    "UA": "Ukraine",
    "RU": "Russland",
    "TR": "Türkei",
    "US": "USA",
    "CA": "Kanada",
    "MX": "Mexiko",
    "BR": "Brasilien",
    "AR": "Argentinien",
    "AU": "Australien",
    "NZ": "Neuseeland",
    "JP": "Japan",
    "CN": "China",
    "KR": "Südkorea",
    "IN": "Indien",
    "PH": "Philippinen",
    "VN": "Vietnam",
    "NG": "Nigeria",
    "ZA": "Südafrika",
    "IL": "Israel",
    UNKNOWN_COUNTRY: "Unbekannt",
}
PROFILE_FIELDS = ("devices", "browsers", "countries")
COUNTER_FIELDS = (
    "sources",
    "entries",
    "languages",
    "pages",
    "programs",
    "program_requests",
)
WWW = "www."


@dataclass
class Totals:
    """The sum of several days; visitors count once per calendar month."""

    days: int = 0
    visitors: int = 0
    page_views: int = 0
    visits: int = 0
    single_page_visits: int = 0
    timed_visits: int = 0
    timed_seconds: int = 0
    counters: dict[str, Counter[str]] = field(
        default_factory=lambda: {
            name: Counter[str]() for name in (*PROFILE_FIELDS, *COUNTER_FIELDS)
        }
    )
    week_hours: list[list[int]] = field(
        default_factory=lambda: [[0] * 24 for _ in range(7)]
    )


def monthly_profiles(summaries: Iterable[DaySummary]) -> list[dict[str, list[str]]]:
    """Visitor ids are stable within a calendar month only: one map per month."""
    months: dict[str, dict[str, list[str]]] = {}
    for summary in summaries:
        months.setdefault(month_of(summary.day), {}).update(summary.profiles)
    return list(months.values())


def unique_visitors(summaries: Iterable[DaySummary]) -> int:
    return sum(len(profiles) for profiles in monthly_profiles(summaries))


def add_up(summaries: Sequence[DaySummary]) -> Totals:
    months = monthly_profiles(summaries)
    totals = Totals(days=len(summaries), visitors=sum(len(m) for m in months))
    for profiles in months:
        for profile in profiles.values():
            for name, value in zip(PROFILE_FIELDS, profile, strict=True):
                totals.counters[name][value] += 1
    for summary in summaries:
        totals.page_views += summary.page_views
        totals.visits += summary.visits
        totals.single_page_visits += summary.single_page_visits
        totals.timed_visits += summary.timed_visits
        totals.timed_seconds += summary.timed_seconds
        for name in COUNTER_FIELDS:
            totals.counters[name].update(getattr(summary, name))
        hours = totals.week_hours[summary.day.weekday()]
        for hour, visitors in enumerate(summary.hourly):
            hours[hour] += visitors
    return totals


def days_between(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


@dataclass(frozen=True)
class Window:
    """A span of days, optionally with the span it is compared to."""

    first: date
    last: date
    previous: tuple[date, date] | None


def _complete(store: Mapping[date, DaySummary], first: date, last: date) -> bool:
    return all(day in store for day in days_between(first, last))


def _select(
    store: Mapping[date, DaySummary], first: date, last: date
) -> list[DaySummary]:
    return [store[day] for day in days_between(first, last) if day in store]


def _ratio(part: float, whole: float) -> float:
    return part / whole if whole else 0.0


def _kpis(current: Totals, previous: Totals | None) -> tuple[Kpi, ...]:
    def before(pick: Callable[[Totals], float]) -> float | None:
        return pick(previous) if previous else None

    per_visit = _ratio(current.page_views, current.visits)
    return (
        Kpi(
            "Besucher",
            current.visitors,
            before(lambda t: t.visitors),
            "count",
            higher_is_better=True,
            note="Geräte, die die Seite geöffnet haben",
        ),
        Kpi(
            "Seitenaufrufe",
            current.page_views,
            before(lambda t: t.page_views),
            "count",
            higher_is_better=True,
            note=f"im Schnitt {per_visit:.1f} Seiten pro Besuch".replace(".", ","),
        ),
        Kpi(
            "Ø Verweildauer",
            _ratio(current.timed_seconds, current.timed_visits),
            before(lambda t: _ratio(t.timed_seconds, t.timed_visits)),
            "seconds",
            higher_is_better=True,
            note="bei Besuchen mit mehr als einer Seite",
        ),
        Kpi(
            "Nur eine Seite angesehen",
            100 * _ratio(current.single_page_visits, current.visits),
            before(lambda t: 100 * _ratio(t.single_page_visits, t.visits)),
            "percent",
            higher_is_better=False,
            note="Anteil der Besuche, die nach der ersten Seite enden",
        ),
    )


def _shares(
    counter: Counter[str], name: Callable[[str], str] = str
) -> tuple[Share, ...]:
    return tuple(
        Share(name(key), value) for key, value in counter.most_common() if value
    )


def domain_entries(entries: Counter[str]) -> tuple[DomainEntry, ...]:
    """Each domain once, its www. name counted beside it."""
    domains: dict[str, list[int]] = {}
    for host, visits in entries.items():
        bare = host.removeprefix(WWW)
        counts = domains.setdefault(bare, [0, 0])
        counts[1 if host.startswith(WWW) else 0] += visits
    ranked = sorted(domains.items(), key=lambda item: -(item[1][0] + item[1][1]))
    return tuple(DomainEntry(domain, plain, www) for domain, (plain, www) in ranked)


def long_date(day: date) -> str:
    return f"{day.day}. {MONTHS[day.month - 1]} {day.year}"


def range_label(first: date, last: date) -> str:
    if first == last:
        return f"{WEEKDAY_NAMES[first.weekday()]}, {long_date(first)}"
    if (first.year, first.month) == (last.year, last.month):
        return f"{first.day}. – {long_date(last)}"
    if first.year == last.year:
        return f"{first.day}. {MONTHS[first.month - 1]} – {long_date(last)}"
    return f"{long_date(first)} – {long_date(last)}"


WEEKDAY_NAMES = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)


def daily_curve(
    store: Mapping[date, DaySummary], first: date, last: date
) -> tuple[Point, ...]:
    return tuple(
        Point(
            f"{day.day}. {MONTHS_SHORT[day.month - 1]}",
            f"{WEEKDAYS[day.weekday()]}, {day.day}. "
            f"{MONTHS_SHORT[day.month - 1]} {day.year}",
            len(store[day].visitors),
        )
        for day in days_between(first, last)
        if day in store
    )


def hourly_curve(summary: DaySummary | None, until_hour: int) -> tuple[Point, ...]:
    hourly = summary.hourly if summary else [0] * 24
    return tuple(
        Point(str(hour), f"{hour}–{hour + 1} Uhr", hourly[hour])
        for hour in range(until_hour + 1)
    )


def monthly_curve(
    store: Mapping[date, DaySummary], year: int, until: date
) -> tuple[Point, ...]:
    points: list[Point] = []
    for month in range(1, until.month + 1):
        days = [
            s for day, s in store.items() if day.year == year and day.month == month
        ]
        if days:
            points.append(
                Point(
                    MONTHS_SHORT[month - 1],
                    f"{MONTHS[month - 1]} {year}",
                    unique_visitors(days),
                )
            )
    return tuple(points)


@dataclass(frozen=True)
class Labels:
    """The texts of one period page."""

    slug: str
    heading: str
    comparison: str
    curve_heading: str


def build_report(
    store: Mapping[date, DaySummary],
    window: Window,
    labels: Labels,
    curve: tuple[Point, ...],
    titles: Mapping[str, str],
) -> PeriodReport:
    current = add_up(_select(store, window.first, window.last))
    previous = None
    if window.previous and _complete(store, *window.previous):
        previous = add_up(_select(store, *window.previous))
    counters = current.counters
    return PeriodReport(
        slug=labels.slug,
        heading=labels.heading,
        range_label=range_label(window.first, window.last),
        comparison_label=labels.comparison,
        curve_heading=labels.curve_heading,
        kpis=_kpis(current, previous),
        curve=curve,
        devices=_shares(counters["devices"]),
        browsers=_shares(counters["browsers"]),
        sources=_shares(counters["sources"]),
        languages=_shares(counters["languages"]),
        pages=_shares(counters["pages"], lambda path: title_for(path, titles)),
        entries=domain_entries(counters["entries"]),
        countries=_shares(
            counters["countries"], lambda code: COUNTRIES.get(code, code)
        ),
        programs=_shares(counters["programs"]),
        program_requests=_shares(counters["program_requests"]),
        week_hours=tuple(tuple(row) for row in current.week_hours),
    )


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _month_end(day: date) -> date:
    following = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following - timedelta(days=1)


def _previous_month_span(today: date) -> tuple[date, date]:
    """The previous month from its first day up to today's day number."""
    start = _month_start(_month_start(today) - timedelta(days=1))
    return start, start.replace(day=min(today.day, _month_end(start).day))


@dataclass(frozen=True)
class Dashboard:
    """Every page of the dashboard: the tabs, their reports and the archive."""

    tabs: tuple[Tab, ...]
    reports: tuple[PeriodReport, ...]
    archive: tuple[PeriodReport, ...]


def _span(last: date, days: int) -> Window:
    first = last - timedelta(days=days - 1)
    before = first - timedelta(days=1)
    return Window(first, last, (before - timedelta(days=days - 1), before))


def current_reports(
    store: Mapping[date, DaySummary], now: datetime, titles: Mapping[str, str]
) -> tuple[PeriodReport, ...]:
    today = now.date()
    month_first = _month_start(today)
    year_first = today.replace(month=1, day=1)
    previous_year = (
        year_first.replace(year=year_first.year - 1),
        _same_day_last_year(today),
    )
    week, month30 = _span(today, 7), _span(today, 30)
    return (
        build_report(
            store,
            Window(today, today, None),
            Labels("heute", "Heute", "", "Besucher pro Stunde"),
            hourly_curve(store.get(today), now.hour),
            titles,
        ),
        build_report(
            store,
            week,
            Labels(
                "7-tage", "Letzte 7 Tage", "ggü. den 7 Tagen davor", "Besucher pro Tag"
            ),
            daily_curve(store, week.first, week.last),
            titles,
        ),
        build_report(
            store,
            month30,
            Labels(
                "index", "Letzte 30 Tage", "ggü. den 30 Tagen davor", "Besucher pro Tag"
            ),
            daily_curve(store, month30.first, month30.last),
            titles,
        ),
        build_report(
            store,
            Window(month_first, today, _previous_month_span(today)),
            Labels(
                "monat",
                f"{MONTHS[today.month - 1]} {today.year}",
                f"ggü. dem Vormonat bis zum {today.day}.",
                "Besucher pro Tag",
            ),
            daily_curve(store, month_first, today),
            titles,
        ),
        build_report(
            store,
            Window(year_first, today, previous_year),
            Labels(
                "jahr",
                f"Jahr {today.year}",
                f"ggü. {today.year - 1} im selben Zeitraum",
                "Besucher pro Monat",
            ),
            monthly_curve(store, today.year, today),
            titles,
        ),
    )


def _same_day_last_year(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def archive_months(store: Mapping[date, DaySummary], today: date) -> list[date]:
    """First days of every month before the current one that has data, newest first."""
    current = _month_start(today)
    return sorted({_month_start(day) for day in store if day < current}, reverse=True)


def archive_reports(
    store: Mapping[date, DaySummary], today: date, titles: Mapping[str, str]
) -> tuple[PeriodReport, ...]:
    reports: list[PeriodReport] = []
    for first in archive_months(store, today):
        last = _month_end(first)
        before_last = first - timedelta(days=1)
        reports.append(
            build_report(
                store,
                Window(first, last, (_month_start(before_last), before_last)),
                Labels(
                    month_of(first),
                    f"{MONTHS[first.month - 1]} {first.year}",
                    "ggü. dem Vormonat",
                    "Besucher pro Tag",
                ),
                daily_curve(store, first, last),
                titles,
            )
        )
    return tuple(reports)


def tabs(today: date) -> tuple[Tab, ...]:
    return (
        Tab("heute", "Heute"),
        Tab("7-tage", "7 Tage"),
        Tab("index", "30 Tage"),
        Tab("monat", MONTHS[today.month - 1]),
        Tab("jahr", str(today.year)),
        Tab("archiv", "Archiv"),
    )


def build_dashboard(
    store: Mapping[date, DaySummary], now: datetime, titles: Mapping[str, str]
) -> Dashboard:
    return Dashboard(
        tabs=tabs(now.date()),
        reports=current_reports(store, now, titles),
        archive=archive_reports(store, now.date(), titles),
    )
