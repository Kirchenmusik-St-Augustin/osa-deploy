"""People or programs: every rule of the classifier and the order they apply in."""

from datetime import timedelta

import pytest

from augustiner_stats.accesslog import Request
from augustiner_stats.classify import (
    DATACENTER_ASNS,
    Category,
    classify_client,
    group_by_client,
    is_hosting_network,
)
from tests.stats_helpers import FIREFOX, GOOGLEBOT, browsing, request


def never(_: str) -> bool:
    return False


def always(_: str) -> bool:
    return True


def reader() -> list[Request]:
    return browsing("2026-11-26 10:00:00", "/", "/pages/musik.html")


def test_a_person_who_runs_the_page_script_is_a_person() -> None:
    assert classify_client(reader(), never) is None


@pytest.mark.parametrize(
    "extra",
    [
        request("2026-11-26 10:05:00", "/wp-login.php", status=404),
        request("2026-11-26 10:05:00", "/.env", status=404),
        request("2026-11-26 10:05:00", "/.git/config", status=404),
        request("2026-11-26 10:05:00", "/cgi-bin/luci", status=404),
        request("2026-11-26 10:05:00", "/backup.sql", status=404),
        request("2026-11-26 10:05:00", "/index.php", status=404),
        request("2026-11-26 10:05:00", "/", method="POST", status=405),
    ],
    ids=["wordpress", "env", "git", "cgi", "sql", "php", "post"],
)
def test_one_scanner_request_marks_the_whole_day(extra: Request) -> None:
    assert classify_client([*reader(), extra], never) is Category.SCANNER


def test_five_different_missing_pages_mark_a_scanner_but_four_do_not() -> None:
    missing = [
        request("2026-11-26 10:05:00", f"/alt/seite-{number}.htm", status=404)
        for number in range(5)
    ]
    assert classify_client([*reader(), *missing[:4]], never) is None
    assert classify_client([*reader(), *missing], never) is Category.SCANNER
    repeated = [request("2026-11-26 10:05:00", "/alt.htm", status=404)] * 6
    assert classify_client([*reader(), *repeated], never) is None


def test_scanning_wins_over_a_search_engine_header() -> None:
    requests = browsing("2026-11-26 10:00:00", "/", agent=GOOGLEBOT)
    requests.append(request("2026-11-26 10:01:00", "/xmlrpc.php", agent=GOOGLEBOT))
    assert classify_client(requests, never) is Category.SCANNER


@pytest.mark.parametrize(
    ("agent", "category"),
    [
        (GOOGLEBOT, Category.SEARCH),
        (
            "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
            Category.SEARCH,
        ),
        (
            (
                "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
                "GPTBot/1.2)"
            ),
            Category.AI,
        ),
        (
            "Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)",
            Category.AI,
        ),
        ("WhatsApp/2.24.1.84 A", Category.PREVIEW),
        (
            "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            Category.PREVIEW,
        ),
        (
            "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)",
            Category.SEO,
        ),
        (
            "Mozilla/5.0+(compatible; UptimeRobot/2.0; http://www.uptimerobot.com/)",
            Category.SEO,
        ),
        ("python-requests/2.32.3 and some padding", Category.TOOL),
        (
            "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/131.0.0.0 Safari/537.36",
            Category.TOOL,
        ),
        ("Mozilla/5.0 zgrab/0.x scanning the internet", Category.TOOL),
        ("curl/8.5.0", Category.TOOL),
        ("", Category.TOOL),
    ],
)
def test_the_header_names_the_program(agent: str, category: Category) -> None:
    requests = browsing("2026-11-26 10:00:00", "/", agent=agent)
    assert classify_client(requests, never) is category


def test_the_search_rule_comes_before_the_ai_rule() -> None:
    both = "Mozilla/5.0 (compatible; Googlebot/2.1) GPTBot"
    assert (
        classify_client(browsing("2026-11-26 10:00:00", "/", agent=both), never)
        is Category.SEARCH
    )


def test_a_client_that_never_runs_the_page_script_is_a_program() -> None:
    pages_only = [
        request("2026-11-26 10:00:00", "/"),
        request("2026-11-26 10:01:00", "/pages/musik.html"),
    ]
    assert classify_client(pages_only, never) is Category.NO_SCRIPT
    images_only = [request("2026-11-26 10:00:00", "/assets/logo.png")]
    assert classify_client(images_only, never) is Category.NO_SCRIPT


def test_thirty_pages_a_minute_are_fine_thirty_one_are_too_fast() -> None:
    step = timedelta(seconds=1)
    thirty = browsing("2026-11-26 10:00:00", *["/"] * 30, step=step)
    assert classify_client(thirty, never) is None
    thirty_one = browsing("2026-11-26 10:00:00", *["/"] * 31, step=step)
    assert classify_client(thirty_one, never) is Category.RAPID


def test_the_minute_window_slides() -> None:
    first = browsing("2026-11-26 10:00:00", *["/"] * 20, step=timedelta(seconds=2))
    later = browsing("2026-11-26 10:01:30", *["/"] * 20, step=timedelta(seconds=2))
    assert classify_client(first + later, never) is None


def test_more_than_three_hundred_pages_a_day_are_too_many() -> None:
    calm = timedelta(minutes=2)
    assert (
        classify_client(browsing("2026-11-26 00:00:00", *["/"] * 300, step=calm), never)
        is None
    )
    assert (
        classify_client(browsing("2026-11-26 00:00:00", *["/"] * 301, step=calm), never)
        is Category.RAPID
    )


@pytest.mark.parametrize(
    "extra",
    [
        request("2026-11-26 10:05:00", "/robots.txt"),
        request("2026-11-26 10:05:00", "/sitemap.xml"),
        request("2026-11-26 10:05:00", "/", protocol="HTTP/1.0"),
    ],
    ids=["robots", "sitemap", "http-1.0"],
)
def test_program_habits_mark_a_program(extra: Request) -> None:
    assert classify_client([*reader(), extra], never) is Category.RAPID


def test_only_head_requests_mark_a_program() -> None:
    heads = [
        request("2026-11-26 10:00:00", "/", method="HEAD"),
        request("2026-11-26 10:00:00", "/data/navigation.json", method="HEAD"),
    ]
    assert classify_client(heads, never) is Category.RAPID


def test_a_datacenter_network_is_checked_last() -> None:
    assert classify_client(reader(), always) is Category.DATACENTER
    assert (
        classify_client(browsing("2026-11-26 10:00:00", "/", agent=GOOGLEBOT), always)
        is Category.SEARCH
    )


def test_private_relay_networks_are_no_datacenters() -> None:
    assert {36183, 13335, 54113}.isdisjoint(DATACENTER_ASNS)
    assert {24940, 16509, 8075}.issubset(DATACENTER_ASNS)


def test_clients_are_address_and_header() -> None:
    requests = [
        *browsing("2026-11-26 10:00:00", "/"),
        *browsing("2026-11-26 10:00:00", "/", agent=GOOGLEBOT),
        *browsing("2026-11-26 10:00:00", "/", ip="198.51.100.4"),
    ]
    clients = group_by_client(requests)
    assert set(clients) == {
        ("203.0.113.17", FIREFOX),
        ("203.0.113.17", GOOGLEBOT),
        ("198.51.100.4", FIREFOX),
    }


def test_every_category_has_a_single_word_token() -> None:
    for category in Category:
        assert category.token
        assert " " not in category.token


@pytest.mark.parametrize(
    ("asn", "operator", "hosting"),
    [
        (24940, "Hetzner Online GmbH", True),
        (64286, "LogicWeb Inc.", True),
        (999_001, "HostRoyale Technologies Pvt Ltd", True),
        (999_002, "Example Cloud Services", True),
        (999_003, "FDCservers.net", True),
        (13335, "Cloudflare, Inc.", False),
        (209242, "Cloudflare London, LLC", False),
        (8692, "Bundesrechenzentrum GmbH", False),
        (8447, "A1 Telekom Austria AG", False),
        (8412, "T-Mobile Austria GmbH", False),
        (999_004, "Ghostwriter Media", False),
    ],
)
def test_hosting_networks_by_number_or_operator_name(
    asn: int, operator: str, hosting: bool
) -> None:
    assert is_hosting_network(asn, operator) is hosting
