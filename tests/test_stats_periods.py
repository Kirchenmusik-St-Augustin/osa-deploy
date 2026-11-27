"""Periods, comparisons and the rendered dashboard pages."""

import re
from collections import Counter
from datetime import date, datetime, timedelta

import pytest

from augustiner_stats.dashboard import (
    DomainEntry,
    Kpi,
    Share,
    Site,
    Tab,
    donut_chart,
    fold_parts,
    format_count,
    format_percent,
    format_seconds,
    kpi_tile,
    nice_step,
    render_archive,
    render_period,
)
from augustiner_stats.days import DaySummary
from augustiner_stats.periods import (
    add_up,
    build_dashboard,
    days_between,
    domain_entries,
    range_label,
    unique_visitors,
)
from tests.stats_helpers import VIENNA

NOW = datetime(2026, 11, 26, 14, 5, tzinfo=VIENNA)
TITLES = {"/": "Startseite", "/pages/musik.html": "Kirchenmusik"}


def day_summary(day: date, visitors: list[str], **values: object) -> DaySummary:
    summary = DaySummary(
        day=day,
        profiles={visitor: ["Handy", "Safari", "AT"] for visitor in visitors},
        page_views=3 * len(visitors),
    )
    summary.visits = len(visitors)
    summary.single_page_visits = len(visitors) // 2
    summary.timed_visits = len(visitors) - summary.single_page_visits
    summary.timed_seconds = 120 * summary.timed_visits
    summary.hourly = [0] * 24
    summary.hourly[10] = len(visitors)
    summary.sources = {"Direkt": len(visitors)}
    summary.entries = {"augustinerkirche.at": len(visitors)}
    summary.languages = {"Deutsch": summary.page_views}
    summary.pages = {"/": summary.page_views}
    summary.programs = {"Suchmaschinen": 2}
    summary.program_requests = {"Suchmaschinen": 5}
    for key, value in values.items():
        setattr(summary, key, value)
    return summary


def store_for(first: date, last: date, visitors: int = 4) -> dict[date, DaySummary]:
    return {
        day: day_summary(day, [f"{day:%m}-{number}" for number in range(visitors)])
        for day in days_between(first, last)
    }


# --- adding up -----------------------------------------------------------------------


def test_visitors_count_once_per_calendar_month() -> None:
    summaries = [
        day_summary(date(2026, 10, 31), ["a", "b"]),
        day_summary(date(2026, 11, 1), ["a", "c"]),
        day_summary(date(2026, 11, 2), ["a", "c", "d"]),
    ]
    assert unique_visitors(summaries) == 2 + 3


def test_adding_up_sums_counters_and_week_hours() -> None:
    monday, tuesday = date(2026, 11, 23), date(2026, 11, 24)
    totals = add_up([day_summary(monday, ["a"]), day_summary(tuesday, ["a", "b"])])
    assert (totals.days, totals.visitors, totals.page_views, totals.visits) == (
        2,
        2,
        9,
        3,
    )
    assert totals.counters["devices"] == Counter({"Handy": 2})
    assert totals.counters["countries"] == Counter({"AT": 2})
    assert totals.week_hours[0][10] == 1
    assert totals.week_hours[1][10] == 2


def test_www_entries_stand_beside_their_domain() -> None:
    entries = domain_entries(
        Counter(
            {
                "augustinerkirche.at": 9,
                "www.augustinerkirche.at": 2,
                "www.hochamt.at": 4,
            }
        )
    )
    assert entries == (
        DomainEntry("augustinerkirche.at", 9, 2),
        DomainEntry("hochamt.at", 0, 4),
    )
    assert entries[0].total == 11


@pytest.mark.parametrize(
    ("first", "last", "label"),
    [
        (date(2026, 11, 26), date(2026, 11, 26), "Donnerstag, 26. November 2026"),
        (date(2026, 11, 1), date(2026, 11, 26), "1. – 26. November 2026"),
        (date(2026, 10, 28), date(2026, 11, 26), "28. Oktober – 26. November 2026"),
        (date(2025, 12, 30), date(2026, 1, 5), "30. Dezember 2025 – 5. Jänner 2026"),
    ],
)
def test_range_labels(first: date, last: date, label: str) -> None:
    assert range_label(first, last) == label


# --- periods -------------------------------------------------------------------------


def test_the_dashboard_has_five_periods_and_the_finished_months() -> None:
    store = store_for(date(2026, 9, 15), NOW.date())
    dashboard = build_dashboard(store, NOW, TITLES)
    assert [tab.slug for tab in dashboard.tabs] == [
        "heute",
        "7-tage",
        "index",
        "monat",
        "jahr",
        "archiv",
    ]
    assert [tab.label for tab in dashboard.tabs][3:5] == ["November", "2026"]
    slugs = [report.slug for report in dashboard.reports]
    assert slugs == ["heute", "7-tage", "index", "monat", "jahr"]
    assert [report.slug for report in dashboard.archive] == ["2026-10", "2026-09"]
    reports = {report.slug: report for report in dashboard.reports}
    assert len(reports["heute"].curve) == NOW.hour + 1
    assert len(reports["7-tage"].curve) == 7
    assert len(reports["index"].curve) == 30
    assert len(reports["monat"].curve) == 26
    assert [point.tick for point in reports["jahr"].curve] == ["Sep.", "Okt.", "Nov."]
    assert reports["index"].pages[0] == Share("Startseite", 30 * 12)


def test_comparisons_appear_only_with_a_complete_previous_period() -> None:
    reports = {
        report.slug: report
        for report in build_dashboard(
            store_for(date(2026, 11, 13), NOW.date()), NOW, TITLES
        ).reports
    }
    assert reports["7-tage"].kpis[0].previous is not None
    assert reports["index"].kpis[0].previous is None
    assert reports["heute"].kpis[0].previous is None
    assert reports["monat"].kpis[0].previous is None


def test_the_month_is_compared_with_the_previous_month_up_to_the_same_day() -> None:
    now = datetime(2026, 3, 31, 9, 0, tzinfo=VIENNA)
    store = store_for(date(2026, 2, 1), now.date())
    month = next(
        r for r in build_dashboard(store, now, TITLES).reports if r.slug == "monat"
    )
    assert month.kpis[0].previous == 4
    assert month.comparison_label == "ggü. dem Vormonat bis zum 31."


def test_the_year_compares_with_the_same_span_of_last_year() -> None:
    now = datetime(2028, 2, 29, 9, 0, tzinfo=VIENNA)
    store = store_for(date(2027, 1, 1), now.date())
    year = next(
        r for r in build_dashboard(store, now, TITLES).reports if r.slug == "jahr"
    )
    assert year.kpis[0].previous == 4 * 2
    assert year.heading == "Jahr 2028"


def test_an_archived_month_compares_with_the_whole_month_before() -> None:
    store = store_for(date(2026, 9, 1), NOW.date())
    october = build_dashboard(store, NOW, TITLES).archive[0]
    assert october.heading == "Oktober 2026"
    assert october.kpis[0].value == 4
    assert october.kpis[0].previous == 4
    assert len(october.curve) == 31


def test_a_new_installation_shows_empty_periods() -> None:
    dashboard = build_dashboard({}, NOW, TITLES)
    assert dashboard.archive == ()
    assert all(report.kpis[0].value == 0 for report in dashboard.reports)


# --- formats and rendering -----------------------------------------------------


def test_number_formats() -> None:
    assert format_count(1234567) == "1.234.567"
    assert format_percent(1, 3) == "33 %"
    assert format_percent(1, 1000) == "<1 %"
    assert format_percent(0, 0) == "0 %"
    assert format_seconds(152) == "2:32 min"
    assert [nice_step(value) for value in (0, 3, 9, 800, 45_000)] == [
        1,
        1,
        3,
        200,
        20000,
    ]


def test_parts_beyond_five_fold_into_others() -> None:
    shares = [Share(f"p{number}", 10 - number) for number in range(7)]
    folded = fold_parts(shares)
    assert [share.label for share in folded] == ["p0", "p1", "p2", "p3", "Sonstige"]
    assert folded[-1].value == 6 + 5 + 4
    assert fold_parts([Share("a", 0), Share("b", 2)]) == [Share("b", 2)]


@pytest.mark.parametrize(
    ("value", "previous", "higher_is_better", "css", "text"),
    [
        (110, 100, True, "delta good", "▲</span> 10"),
        (90, 100, True, "delta bad", "▼</span> 10"),
        (40, 50, False, "delta good", "▼</span> 20"),
        (100.2, 100, True, "delta", "± 0"),
    ],
)
def test_kpi_tiles_color_the_change_by_whether_more_is_good(
    value: float, previous: float, higher_is_better: bool, css: str, text: str
) -> None:
    tile = kpi_tile(
        Kpi("x", value, previous, "count", higher_is_better=higher_is_better, note="n"),
        "ggü. vorher",
    )
    assert f'class="{css}"' in tile
    assert text in tile


def test_a_kpi_without_previous_value_shows_no_change() -> None:
    assert "delta" not in kpi_tile(
        Kpi("x", 1, None, "count", higher_is_better=True, note=""), ""
    )


def test_rendered_pages_link_from_the_root_and_carry_no_script() -> None:
    store = store_for(date(2026, 9, 1), NOW.date())
    dashboard = build_dashboard(store, NOW, TITLES)
    site = Site("Statistik", dashboard.tabs, NOW, preview=False, has_geodata=True)
    pages = [render_period(site, report) for report in dashboard.reports]
    pages.append(render_period(site, dashboard.archive[0], archived=True))
    pages.append(render_archive(site, [Tab("2026-10", "Oktober 2026")]))
    for page in pages:
        assert "<script" not in page
        links = re.findall(r'href="([^"]+)"', page)
        internal = [link for link in links if not link.startswith(("https://", "#"))]
        assert internal
        assert all(link.startswith("/") for link in internal)
    current = pages[2]
    assert current.index('class="scope"') < current.index('class="tiles"')
    assert 'id="automatisch"' in current
    assert "Aus welchen Ländern?" in current
    assert "IP Geolocation by DB-IP" in current
    assert "wird einmal pro Stunde aktualisiert" in current
    assert "Vorschau mit erfundenen Testdaten" not in current
    archived = pages[-2]
    assert "← Zurück zum Archiv" in archived
    assert "die Zahlen ändern sich nicht mehr" in archived
    assert '<a href="/archiv.html" aria-current="page">' in archived


def test_programs_count_as_visits_and_their_requests_stay_in_the_table() -> None:
    """A scanner asking 4,500 times is one visit, not 4,500 attacks."""
    store = {
        day: day_summary(
            day,
            ["a"],
            programs={"Suche nach Sicherheitslücken": 3, "Suchmaschinen": 1},
            program_requests={"Suche nach Sicherheitslücken": 4500, "Suchmaschinen": 7},
        )
        for day in days_between(date(2026, 11, 25), NOW.date())
    }
    dashboard = build_dashboard(store, NOW, TITLES)
    site = Site("Statistik", dashboard.tabs, NOW, preview=False, has_geodata=True)
    week = next(report for report in dashboard.reports if report.slug == "7-tage")
    page = render_period(site, week)
    card = page[page.index('id="automatisch"') :]
    bars = card[: card.index("</ol>")]
    assert re.findall(r'<span class="count">([^<]+)</span>', bars) == ["6", "2"]
    assert "Besuche" in bars
    assert "9.000" not in bars
    header = "<th>Art</th><th>Besuche</th><th>Anfragen</th><th>Anteil der Besuche</th>"
    assert header in card
    assert "<td>Suche nach Sicherheitslücken</td><td>6</td><td>9.000</td>" in card
    assert "einmal pro Tag als Besuch" in card
    assert "Angriff" not in page


def test_without_geodata_the_country_card_and_credit_are_left_out() -> None:
    dashboard = build_dashboard(store_for(date(2026, 11, 20), NOW.date()), NOW, TITLES)
    site = Site("Statistik", dashboard.tabs, NOW, preview=True, has_geodata=False)
    page = render_period(site, dashboard.reports[0])
    assert "Aus welchen Ländern?" not in page
    assert "DB-IP" not in page
    assert "Vorschau mit erfundenen Testdaten" in page


def test_labels_are_escaped() -> None:
    dashboard = build_dashboard(
        {NOW.date(): day_summary(NOW.date(), ["a"], pages={"/<b>.html": 1})},
        NOW,
        {},
    )
    site = Site("S", dashboard.tabs, NOW, preview=False, has_geodata=False)
    page = render_period(site, dashboard.reports[0])
    assert "<b>" not in page
    assert "&lt;b&gt;" in page


def test_an_empty_archive_says_so() -> None:
    site = Site("S", (), NOW, preview=False, has_geodata=False)
    assert "Noch kein Monat abgeschlossen." in render_archive(site, [])


def test_days_between_is_inclusive() -> None:
    assert days_between(date(2026, 11, 1), date(2026, 11, 3)) == [
        date(2026, 11, 1) + timedelta(days=offset) for offset in range(3)
    ]


def test_a_ring_of_several_parts_has_a_gap_before_each_part() -> None:
    page = donut_chart(
        [Share("Handy", 6), Share("Computer", 3), Share("Tablet", 1)], "Besucher"
    )
    assert page.count('class="gap"') == 3
    assert page.count("<circle") == 3
    assert "Handy: 6 Besucher (60\u202f%)" in page
    single = donut_chart([Share("Handy", 6)], "Besucher")
    assert 'class="gap"' not in single


def test_an_empty_dashboard_renders() -> None:
    dashboard = build_dashboard({}, NOW, TITLES)
    site = Site("S", dashboard.tabs, NOW, preview=False, has_geodata=True)
    page = render_period(site, dashboard.reports[2])
    assert 'class="baseline"' in page
    assert 'class="dot"' not in page


def test_mid_month_the_previous_month_counts_only_up_to_the_same_day() -> None:
    store = store_for(date(2026, 10, 1), NOW.date())
    month = next(
        r for r in build_dashboard(store, NOW, TITLES).reports if r.slug == "monat"
    )
    page_views = month.kpis[1]
    assert page_views.value == 26 * 12
    assert page_views.previous == 26 * 12
    assert month.comparison_label == "ggü. dem Vormonat bis zum 26."


def test_charts_count_each_visitor_once_per_month_like_the_tile() -> None:
    november = [
        day_summary(date(2026, 11, 2), ["a", "b"]),
        day_summary(
            date(2026, 11, 3), ["a"], profiles={"a": ["Handy", "Safari", "AT"]}
        ),
        day_summary(
            date(2026, 12, 1), ["a"], profiles={"a": ["Handy", "Safari", "AT"]}
        ),
    ]
    totals = add_up(november)
    assert totals.visitors == 3
    assert sum(totals.counters["devices"].values()) == totals.visitors
    assert sum(totals.counters["browsers"].values()) == totals.visitors
    assert sum(totals.counters["countries"].values()) == totals.visitors
