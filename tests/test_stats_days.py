"""Pseudonyms, the day summary, its storage and the GoAccess lines."""

import ipaddress
import json
import stat
from datetime import date, timedelta
from pathlib import Path

import pytest

from augustiner_stats.accesslog import Request
from augustiner_stats.days import (
    CAMPAIGN,
    DIRECT,
    OTHER_SITES,
    SEARCH,
    SOCIAL,
    DaySummary,
    Person,
    Program,
    goaccess_line,
    kept_query,
    read_summary,
    source_of,
    summarize,
    summary_from_json,
    summary_json,
    write_summary,
)
from augustiner_stats.pseudonym import (
    UNKNOWN_ADDRESS,
    MonthSecrets,
    month_of,
    pseudonymous_address,
    visitor_id,
)
from tests.stats_helpers import IPHONE, at, browsing, request

DAY = date(2026, 11, 26)
OWN = frozenset({"augustinerkirche.at", "www.augustinerkirche.at", "hochamt.at"})

# --- pseudonyms -------------------------------------------------------------------


def test_a_month_has_one_secret_readable_by_the_owner_only(tmp_path: Path) -> None:
    secrets = MonthSecrets(tmp_path)
    first = secrets.for_day(date(2026, 11, 1))
    assert len(first) == 32
    assert secrets.for_day(date(2026, 11, 30)) == first
    assert secrets.for_day(date(2026, 12, 1)) != first
    path = tmp_path / "secret-2026-11"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert secrets.months() == ["2026-11", "2026-12"]
    secrets.forget("2026-11")
    secrets.forget("2026-10")
    assert secrets.months() == ["2026-12"]
    assert month_of(DAY) == "2026-11"


def test_a_pseudonymous_address_keeps_only_the_network() -> None:
    secret, other = b"a" * 32, b"b" * 32
    value = pseudonymous_address("91.115.17.77", secret)
    assert value.startswith("91.115.17.")
    assert value == pseudonymous_address("91.115.17.77", secret)
    assert {pseudonymous_address("91.115.17.77", key) for key in (secret, other)} != {
        value
    }
    six = ipaddress.IPv6Address(
        pseudonymous_address("2a02:8388:1:2:abcd:ef01:2345:6789", secret)
    )
    assert int(six) >> 80 == int(ipaddress.IPv6Address("2a02:8388:1::")) >> 80
    assert six != ipaddress.IPv6Address("2a02:8388:1:2:abcd:ef01:2345:6789")
    assert pseudonymous_address("::ffff:91.115.17.77", secret) == value
    assert pseudonymous_address("nonsense", secret) == UNKNOWN_ADDRESS


def test_many_addresses_of_one_network_stay_mostly_apart() -> None:
    secret = b"s" * 32
    values = {pseudonymous_address(f"91.115.17.{host}", secret) for host in range(40)}
    assert len(values) > 30


def test_visitor_ids_depend_on_address_header_and_month() -> None:
    first = visitor_id("91.115.17.77", IPHONE, b"a" * 32)
    assert len(first) == 16
    assert first == visitor_id("91.115.17.77", IPHONE, b"a" * 32)
    assert first != visitor_id("91.115.17.78", IPHONE, b"a" * 32)
    assert first != visitor_id("91.115.17.77", "other", b"a" * 32)
    assert first != visitor_id("91.115.17.77", IPHONE, b"b" * 32)


# --- sources ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("referer", "query", "expected"),
    [
        ("", "", DIRECT),
        ("https://augustinerkirche.at/pages/musik.html", "", DIRECT),
        ("https://www.google.com/", "", SEARCH),
        ("https://www.google.at/", "", SEARCH),
        ("https://duckduckgo.com/", "", SEARCH),
        ("https://www.ecosia.org/", "", SEARCH),
        ("https://l.facebook.com/", "", SOCIAL),
        ("https://www.instagram.com/", "", SOCIAL),
        ("https://t.co/abc", "", SOCIAL),
        ("https://www.erzdioezese-wien.at/", "", OTHER_SITES),
        ("https://www.google.com/", "utm_source=pfarrblatt&utm_medium=print", CAMPAIGN),
    ],
)
def test_where_a_visit_came_from(referer: str, query: str, expected: str) -> None:
    view = request("2026-11-26 10:00:00", "/", referer=referer, query=query)
    assert source_of(view, OWN) == expected


# --- the day summary ---------------------------------------------------------------


def person(visitor: str, requests: list[Request], country: str = "AT") -> Person:
    return Person(visitor=visitor, country=country, requests=requests)


def test_a_day_is_summed_up_per_visitor_and_visit() -> None:
    morning = browsing(
        "2026-11-26 09:00:00",
        "/",
        "/pages/musik.html",
        referer="https://www.google.at/",
    )
    evening = browsing("2026-11-26 18:00:00", "/en/pages/musik.html")
    redirect = request(
        "2026-11-26 20:00:00",
        "/",
        host="hochamt.at",
        status=302,
        agent=IPHONE,
        ip="198.51.100.4",
    )
    phone = [
        redirect,
        *browsing(
            "2026-11-26 20:00:01", "/pages/musik.html", agent=IPHONE, ip="198.51.100.4"
        ),
    ]
    summary = summarize(
        DAY,
        [person("a", morning + evening), person("b", phone, country="DE")],
        [Program("Suchmaschinen", 12), Program("Suchmaschinen", 3)],
        OWN,
    )
    assert summary.visitors == ["a", "b"]
    assert summary.profiles == {
        "a": ["Computer", "Firefox", "AT"],
        "b": ["Handy", "Safari", "DE"],
    }
    assert summary.page_views == 4
    assert summary.visits == 3
    assert summary.single_page_visits == 2
    assert (summary.timed_visits, summary.timed_seconds) == (1, 60)
    assert summary.entries == {"augustinerkirche.at": 2, "hochamt.at": 1}
    assert summary.sources == {SEARCH: 1, DIRECT: 2}
    assert summary.languages == {"Deutsch": 3, "Englisch": 1}
    assert summary.pages == {"/": 1, "/pages/musik.html": 3}
    assert summary.hourly[9] == 1
    assert summary.hourly[18] == 1
    assert summary.hourly[20] == 1
    assert sum(summary.hourly) == 3
    assert summary.programs == {"Suchmaschinen": 2}
    assert summary.program_requests == {"Suchmaschinen": 15}


def test_thirty_minutes_apart_is_still_one_visit_thirty_one_are_two() -> None:
    same = browsing(
        "2026-11-26 09:00:00", "/", "/pages/a.html", step=timedelta(minutes=30)
    )
    assert summarize(DAY, [person("a", same)], [], OWN).visits == 1
    split = browsing(
        "2026-11-26 09:00:00", "/", "/pages/a.html", step=timedelta(minutes=31)
    )
    assert summarize(DAY, [person("a", split)], [], OWN).visits == 2


def test_a_client_without_page_views_is_no_visitor() -> None:
    only_data = [request("2026-11-26 09:00:00", "/data/navigation.json")]
    summary = summarize(DAY, [person("a", only_data)], [], OWN)
    assert summary.visitors == []
    assert summary.visits == 0
    assert summary.profiles == {}


def test_a_summary_survives_storage(tmp_path: Path) -> None:
    summary = summarize(
        DAY,
        [person("a", browsing("2026-11-26 09:00:00", "/"))],
        [Program("KI-Dienste", 3)],
        OWN,
    )
    assert summary.programs == {"KI-Dienste": 1}
    write_summary(tmp_path / "day.json", summary)
    assert read_summary(tmp_path / "day.json") == summary
    assert summary_from_json(summary_json(summary)) == summary


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("visits", "1", "visits is not a count"),
        ("visits", True, "visits is not a count"),
        ("hourly", [0], "hourly must have 24 entries"),
        ("hourly", "x", "hourly is not a list"),
        ("sources", {"Direkt": -1}, "sources must map text to counts"),
        ("sources", [], "sources is not a mapping"),
        ("profiles", [], "profiles is not a mapping"),
        ("profiles", {"a": ["Handy", "Safari"]}, "profiles must map visitor ids"),
        ("profiles", {"a": ["Handy", "Safari", 1]}, "profiles must map visitor ids"),
    ],
)
def test_a_damaged_summary_is_refused(key: str, value: object, message: str) -> None:
    data = json.loads(summary_json(DaySummary(day=DAY)))
    data[key] = value
    with pytest.raises(TypeError, match=message):
        summary_from_json(json.dumps(data))


@pytest.mark.parametrize(
    ("text", "message"),
    [("[]", "a day summary is a JSON object"), ("{}", "day is missing")],
)
def test_a_summary_must_be_an_object_with_a_day(text: str, message: str) -> None:
    with pytest.raises(TypeError, match=message):
        summary_from_json(text)


# --- GoAccess lines ----------------------------------------------------------------


def test_a_goaccess_line_has_the_documented_fields() -> None:
    view = request(
        "2026-11-26 10:00:00",
        "/pages/musik.html",
        query="utm_source=pfarrblatt&token=geheim&utm_medium=print",
        referer="https://www.google.at/search?q=messe",
    )
    line = goaccess_line(view, "203.0.113.5", "-", "Mozilla/5.0 (X11; Linux x86_64)")
    assert line == (
        "augustinerkirche.at 203.0.113.5 - [2026-11-26:10:00:00] "
        '"GET /pages/musik.html?utm_source=pfarrblatt&utm_medium=print HTTP/2.0" '
        "200 5120 "
        '"https://www.google.at/search" "Mozilla/5.0 (X11; Linux x86_64)" 1500'
    )


def test_quotes_and_controls_cannot_break_a_line() -> None:
    view = request(
        "2026-11-26 10:00:00",
        "/a b",
        agent='Evil" 200 1 "x\nnext',
        referer="not a url",
        protocol="",
    )
    line = goaccess_line(view, "203.0.113.5", "Werkzeug/Skript", view.user_agent)
    assert "\n" not in line
    assert '"GET /a%20b HTTP/1.1"' in line
    assert '"-" "Evil 200 1 xnext"' in line
    assert goaccess_line(
        request("2026-11-26 10:00:00", agent=""), "1.2.3.4", "-", ""
    ).endswith('"-" 1500')


def test_only_campaign_parameters_are_kept() -> None:
    assert kept_query("utm_source=a&email=b%40c&utm_campaign=advent") == (
        "utm_source=a&utm_campaign=advent"
    )
    assert kept_query("token=x") == ""


def test_the_day_constant_is_a_real_date() -> None:
    assert at("2026-11-26 00:00:00").date() == DAY


def test_a_redirect_without_a_page_view_is_no_visit() -> None:
    redirect = request("2026-11-26 07:00:00", "/", host="hochamt.at", status=302)
    later = browsing("2026-11-26 09:00:00", "/pages/musik.html")
    summary = summarize(DAY, [person("a", [redirect, *later])], [], OWN)
    assert summary.visits == 1
    assert summary.entries == {"augustinerkirche.at": 1}
