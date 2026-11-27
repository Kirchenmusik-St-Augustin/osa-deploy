"""Log parsing, browser detection, address tables and page rules of the statistics."""

import gzip
import json
from pathlib import Path

import pytest

from augustiner_stats.accesslog import parse_line, read_unfinished
from augustiner_stats.ipranges import AddressLookup, RangeTable, write_table
from augustiner_stats.pages import (
    is_page_view,
    language,
    page_of,
    page_titles,
    title_for,
)
from augustiner_stats.useragent import parse_agent, reduced_header
from tests.stats_helpers import (
    FIREFOX,
    IPHONE,
    VIENNA,
    WINDOWS_CHROME,
    at,
    caddy_line,
    request,
)

SAMSUNG_PHONE = (
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like"
    " Gecko) SamsungBrowser/26.0 Chrome/122.0.0.0 Mobile Safari/537.36"
)
ANDROID_TABLET = (
    "Mozilla/5.0 (Linux; Android 13; SM-X200) AppleWebKit/537.36 (KHTML, like"
    " Gecko) Chrome/131.0.0.0 Safari/537.36"
)
IPAD = (
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML,"
    " like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
MAC_SAFARI = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
    " (KHTML, like Gecko) Version/18.2 Safari/605.1.15"
)

# --- access log ----------------------------------------------------------------


def test_a_log_line_becomes_a_request_in_vienna_time() -> None:
    line = caddy_line(
        at("2026-11-26 10:00:00"),
        host="AugustinerKirche.at:443",
        uri="/pages/musik.html?utm_source=pfarrblatt",
        referer="https://www.google.com/",
    )
    parsed = parse_line(line, VIENNA)
    assert parsed is not None
    assert parsed.time == at("2026-11-26 10:00:00")
    assert parsed.host == "augustinerkirche.at"
    assert parsed.path == "/pages/musik.html"
    assert parsed.query == "utm_source=pfarrblatt"
    assert parsed.referer == "https://www.google.com/"
    assert parsed.user_agent == FIREFOX
    assert (parsed.status, parsed.size, parsed.protocol) == (200, 5120, "HTTP/2.0")
    assert parsed.duration == pytest.approx(0.0015)


def test_the_remote_address_stands_in_for_a_missing_client_address() -> None:
    record = json.loads(caddy_line(at("2026-11-26 10:00:00")))
    del record["request"]["client_ip"]
    record["request"]["remote_ip"] = "198.51.100.9"
    parsed = parse_line(json.dumps(record), VIENNA)
    assert parsed is not None
    assert parsed.ip == "198.51.100.9"


def test_missing_headers_leave_empty_texts() -> None:
    record = json.loads(caddy_line(at("2026-11-26 10:00:00"), agent=""))
    record["request"]["headers"] = {"User-Agent": [], "Referer": [7]}
    del record["request"]["proto"]
    parsed = parse_line(json.dumps(record), VIENNA)
    assert parsed is not None
    assert (parsed.user_agent, parsed.referer, parsed.protocol) == ("", "", "")


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[1, 2]",
        json.dumps({"ts": 1, "status": 200}),
        json.dumps({"ts": True, "status": 200, "request": {}}),
        json.dumps(
            {
                "ts": 1795705854.0,
                "status": 200,
                "request": {"client_ip": "1.2.3.4", "host": "a.at", "method": "GET"},
            }
        ),
    ],
    ids=["no-json", "not-an-object", "no-request", "boolean-time", "no-uri"],
)
def test_incomplete_lines_are_refused(line: str) -> None:
    assert parse_line(line, VIENNA) is None


def test_reading_skips_finished_days_and_counts_bad_lines(tmp_path: Path) -> None:
    rolled = tmp_path / "access-2026-11-25T23-00-00.000.log.gz"
    with gzip.open(rolled, "wt", encoding="utf-8") as stream:
        stream.write(caddy_line(at("2026-11-25 22:00:00"), uri="/old") + "\n")
        stream.write(caddy_line(at("2026-11-26 00:10:00"), uri="/b") + "\n")
    (tmp_path / "access.log").write_text(
        "\n".join(
            [
                caddy_line(at("2026-11-26 00:05:00"), uri="/a"),
                "garbage",
                "",
                caddy_line(at("2026-11-27 08:00:00"), uri="/c"),
            ]
        ),
        encoding="utf-8",
    )
    reading = read_unfinished(tmp_path, {at("2026-11-25 12:00:00").date()}, VIENNA)
    assert reading.malformed == 1
    assert sorted(reading.days) == [
        at("2026-11-26 00:00:00").date(),
        at("2026-11-27 00:00:00").date(),
    ]
    assert [r.path for r in reading.days[at("2026-11-26 00:00:00").date()]] == [
        "/a",
        "/b",
    ]


# --- browsers ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "browser", "major", "system", "device"),
    [
        (IPHONE, "Safari", 18, "iOS", "Handy"),
        (WINDOWS_CHROME, "Chrome", 131, "Windows", "Computer"),
        (FIREFOX, "Firefox", 133, "Linux", "Computer"),
        (WINDOWS_CHROME + " Edg/131.0.2903.70", "Edge", 131, "Windows", "Computer"),
        (WINDOWS_CHROME + " OPR/115.0.0.0", "Opera", 115, "Windows", "Computer"),
        (
            SAMSUNG_PHONE,
            "Samsung Internet",
            26,
            "Android",
            "Handy",
        ),
        (
            ANDROID_TABLET,
            "Chrome",
            131,
            "Android",
            "Tablet",
        ),
        (
            IPAD,
            "Safari",
            17,
            "iPadOS",
            "Tablet",
        ),
        (
            MAC_SAFARI,
            "Safari",
            18,
            "macOS",
            "Computer",
        ),
        (
            "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) Chrome/130.0",
            "Chrome",
            130,
            "ChromeOS",
            "Computer",
        ),
        ("Lynx/2.9.0", "Andere", None, "Andere", "Computer"),
    ],
)
def test_the_browser_family_and_device_are_recognised(
    header: str, browser: str, major: int | None, system: str, device: str
) -> None:
    agent = parse_agent(header)
    assert (agent.browser, agent.major, agent.system, agent.device) == (
        browser,
        major,
        system,
        device,
    )


def test_the_reduced_header_keeps_only_family_version_and_system() -> None:
    header = reduced_header(parse_agent(IPHONE))
    assert header.startswith("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)")
    assert "Version/18.0 Mobile Safari" in header
    assert "18_1" not in header
    assert "15E148" not in header
    unknown = reduced_header(parse_agent("Lynx/2.9.0"))
    assert unknown == "Mozilla/5.0 (unknown)"


@pytest.mark.parametrize(
    ("header", "token"),
    [
        (WINDOWS_CHROME, "Chrome/131.0.0.0"),
        (WINDOWS_CHROME + " Edg/131.0", "Edg/131.0.0.0"),
        (WINDOWS_CHROME + " OPR/115.0", "OPR/115.0.0.0"),
        (FIREFOX, "Firefox/133.0"),
        (
            "Mozilla/5.0 (Linux; Android 14) SamsungBrowser/26.0 Chrome/1 Mobile",
            "SamsungBrowser/26.0",
        ),
    ],
)
def test_the_reduced_header_names_the_browser_for_goaccess(
    header: str, token: str
) -> None:
    assert token in reduced_header(parse_agent(header))


# --- address tables --------------------------------------------------------------


def test_ranges_are_found_in_both_families(tmp_path: Path) -> None:
    v4, v6 = tmp_path / "v4.bin", tmp_path / "v6.bin"
    assert (
        write_table(
            v4, 4, [(0x01000000, 0x010000FF, "AU"), (0x5B731100, 0x5B7311FF, "AT")]
        )
        == 2
    )
    write_table(v6, 6, [(0x2A028388 << 96, ((0x2A028388 + 1) << 96) - 1, "DE")])
    lookup = AddressLookup(v4, v6)
    assert lookup.available
    assert lookup.lookup("91.115.17.77") == "AT"
    assert lookup.lookup("1.0.0.255") == "AU"
    assert lookup.lookup("1.0.1.0") is None
    assert lookup.lookup("0.0.0.1") is None
    assert lookup.lookup("::ffff:91.115.17.1") == "AT"
    assert lookup.lookup("2a02:8388::1") == "DE"
    assert lookup.lookup("not an address") is None
    lookup.close()


def test_missing_tables_answer_nothing(tmp_path: Path) -> None:
    lookup = AddressLookup(tmp_path / "v4.bin", tmp_path / "v6.bin")
    assert not lookup.available
    assert lookup.lookup("91.115.17.77") is None


@pytest.mark.parametrize(
    "rows",
    [[(5, 9, "AT"), (9, 12, "DE")], [(5, 4, "AT")], [(1, 2, "AUT")]],
    ids=["overlapping", "reversed", "long-value"],
)
def test_malformed_ranges_are_refused(
    tmp_path: Path, rows: list[tuple[int, int, str]]
) -> None:
    with pytest.raises(ValueError, match="malformed or out of order"):
        write_table(tmp_path / "t.bin", 4, rows)
    assert not (tmp_path / "t.bin").exists()


def test_a_table_of_the_other_family_is_refused(tmp_path: Path) -> None:
    write_table(tmp_path / "t.bin", 4, [(1, 2, "AT")])
    with pytest.raises(ValueError, match="not a range table for IPv6"):
        RangeTable(tmp_path / "t.bin", 6)
    table = RangeTable(tmp_path / "t.bin", 4)
    assert len(table) == 1
    table.close()


# --- pages ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "method", "status", "expected"),
    [
        ("/", "GET", 200, True),
        ("/pages/musik.html", "GET", 304, True),
        ("/en", "GET", 200, True),
        ("/en/", "GET", 200, True),
        ("/pages/musik.html", "HEAD", 200, False),
        ("/pages/musik.html", "GET", 404, False),
        ("/pages/musik.html", "GET", 206, False),
        ("/offline.html", "GET", 200, False),
        ("/css/style.css", "GET", 200, False),
        ("/data/navigation.json", "GET", 200, False),
        ("/english", "GET", 200, False),
    ],
)
def test_page_views(path: str, method: str, status: int, expected: bool) -> None:
    view = request("2026-11-26 10:00:00", path, method=method, status=status)
    assert is_page_view(view) is expected


@pytest.mark.parametrize(
    ("path", "lang", "page"),
    [
        ("/", "Deutsch", "/"),
        ("/index.html", "Deutsch", "/"),
        ("/pages/musik.html", "Deutsch", "/pages/musik.html"),
        ("/en", "Englisch", "/"),
        ("/en/index.html", "Englisch", "/"),
        ("/it/pages/musik.html", "Italienisch", "/pages/musik.html"),
        ("/fr/", "Französisch", "/"),
        ("/es/pages/kontakt.html", "Spanisch", "/pages/kontakt.html"),
    ],
)
def test_language_and_page(path: str, lang: str, page: str) -> None:
    assert language(path) == lang
    assert page_of(path) == page


def test_titles_come_from_the_pages_or_their_file_names(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    (pages / "runden").mkdir(parents=True)
    (pages / "musik.html").write_text(
        "<html><title>Kirchenmusik — Augustinerkirche Wien</title></html>",
        encoding="utf-8",
    )
    (pages / "gottesdienste.html").write_text(
        "<title>Augustinerkirche Wien</title>", encoding="utf-8"
    )
    (pages / "orgel-reil.html").write_text("<p>no title</p>", encoding="utf-8")
    (pages / "runden" / "bibel.html").write_text(
        "<TITLE>Bibel &amp; Brot — Augustinerkirche Wien</TITLE>", encoding="utf-8"
    )
    titles = page_titles(tmp_path, "Augustinerkirche Wien")
    assert titles == {
        "/": "Startseite",
        "/pages/gottesdienste.html": "Gottesdienste",
        "/pages/musik.html": "Kirchenmusik",
        "/pages/orgel-reil.html": "Orgel reil",
        "/pages/runden/bibel.html": "Bibel & Brot",
    }
    assert title_for("/pages/neu.html", titles) == "Neu"
    assert page_titles(tmp_path / "missing", "x") == {"/": "Startseite"}
