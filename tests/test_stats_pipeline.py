"""The hourly run end to end, the geodata job and the command line."""

import gzip
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import URLError

import pytest

from augustiner_stats import __main__ as cli
from augustiner_stats import geodata
from augustiner_stats.geodata import GeodataError, fetch, update
from augustiner_stats.ipranges import AddressLookup, write_table
from augustiner_stats.pipeline import (
    PEOPLE_DAYS,
    PROGRAM_DAYS,
    Geodata,
    Layout,
    NotEnoughSpaceError,
    Settings,
    run,
)
from tests.stats_helpers import FIREFOX, GOOGLEBOT, VIENNA, at, caddy_line

NOW = datetime(2026, 11, 26, 14, 5, tzinfo=VIENNA)
READER = "203.0.113.17"
CLOUD = "198.51.100.40"
HOSTS = frozenset({"augustinerkirche.at", "www.augustinerkirche.at", "hochamt.at"})


def reading(when: str, ip: str = READER, agent: str = FIREFOX) -> list[str]:
    moment = at(when)
    return [
        caddy_line(moment, ip=ip, agent=agent, uri="/pages/musik.html"),
        caddy_line(moment, ip=ip, agent=agent, uri="/data/navigation.json"),
    ]


def write_log(directory: Path, lines: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "access.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def install_geo(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    write_table(directory / "country-v4.bin", 4, [(0xCB007100, 0xCB0071FF, "AT")])
    write_table(directory / "country-v6.bin", 6, [])
    write_table(directory / "datacenter-v4.bin", 4, [(0xC6336400, 0xC63364FF, "DC")])
    write_table(directory / "datacenter-v6.bin", 6, [])


def settings(tmp_path: Path, min_free_mb: int = 0) -> Settings:
    site = tmp_path / "site" / "pages"
    site.mkdir(parents=True, exist_ok=True)
    (site / "musik.html").write_text(
        "<title>Kirchenmusik — Augustinerkirche Wien</title>"
    )
    return Settings(
        logs=tmp_path / "logs",
        data=tmp_path / "data",
        www=tmp_path / "www",
        site=tmp_path / "site",
        own_hosts=HOSTS,
        zone=VIENNA,
        min_free_mb=min_free_mb,
    )


@pytest.fixture
def stage(tmp_path: Path) -> Settings:
    config = settings(tmp_path)
    install_geo(config.data / "geo")
    write_log(
        config.logs,
        [
            *reading("2026-10-31 20:00:00"),
            *reading("2026-11-25 09:00:00"),
            *reading("2026-11-25 09:00:00", agent=GOOGLEBOT),
            caddy_line(
                at("2026-11-25 09:30:00"),
                ip="192.0.2.9",
                uri="/wp-login.php",
                status=404,
            ),
            *reading("2026-11-25 11:00:00", ip=CLOUD),
            *reading("2026-11-26 10:00:00"),
            *reading("2026-11-27 10:00:00"),
            "not a log line",
        ],
    )
    return config


def test_a_run_closes_past_days_and_renders_the_dashboard(stage: Settings) -> None:
    run(stage, NOW, Geodata.load)
    layout = Layout(stage.data)
    assert layout.finished_days() == {date(2026, 10, 31), date(2026, 11, 25)}
    queue = (layout.state / "goaccess-queue").read_text(encoding="utf-8").split()
    assert queue == ["2026-10-31", "2026-11-25"]
    summary = json.loads((layout.days / "2026-11-25.json").read_text(encoding="utf-8"))
    assert [profile[2] for profile in summary["profiles"].values()] == ["AT"]
    assert summary["page_views"] == 1
    assert summary["program_requests"] == {
        "Suchmaschinen": 2,
        "Suche nach Sicherheitslücken": 1,
        "Aus Rechenzentren (Programme)": 2,
    }
    assert summary["programs"] == {
        "Suchmaschinen": 1,
        "Suche nach Sicherheitslücken": 1,
        "Aus Rechenzentren (Programme)": 1,
    }
    with gzip.open(layout.days / "2026-11-25.menschen.log.gz", "rt") as stream:
        people = stream.read().splitlines()
    assert len(people) == 2
    assert all(line.split()[1].startswith("203.0.113.") for line in people)
    assert all(READER not in line for line in people)
    assert all("rv:133.0" not in line for line in people)
    with gzip.open(layout.days / "2026-11-25.automatisch.log.gz", "rt") as stream:
        programs = stream.read()
    assert "Suchmaschine" in programs
    assert " Scanner [" in programs
    assert (layout.current / "menschen.log").read_text().count("\n") == 2
    assert (layout.current / "today.json").is_file()
    assert [path.name for path in layout.state.glob("secret-*")] == ["secret-2026-11"]
    for page in ("index", "heute", "7-tage", "monat", "jahr", "archiv"):
        assert (stage.www / f"{page}.html").is_file()
    assert (stage.www / "archiv" / "2026-10.html").is_file()
    assert "Kirchenmusik" in (stage.www / "index.html").read_text(encoding="utf-8")


def test_a_second_run_closes_nothing_twice(stage: Settings) -> None:
    run(stage, NOW, Geodata.load)
    run(stage, NOW + timedelta(hours=1), Geodata.load)
    queue = (
        (Layout(stage.data).state / "goaccess-queue")
        .read_text(encoding="utf-8")
        .split()
    )
    assert queue == ["2026-10-31", "2026-11-25"]


def test_a_quiet_day_leaves_empty_current_files(tmp_path: Path) -> None:
    config = settings(tmp_path)
    write_log(config.logs, reading("2026-11-25 09:00:00"))
    run(config, NOW, Geodata.load)
    layout = Layout(config.data)
    assert (layout.current / "menschen.log").read_text() == ""
    assert not (layout.current / "today.json").exists()
    assert "Aus welchen Ländern?" not in (config.www / "index.html").read_text(
        encoding="utf-8"
    )


def test_old_files_expire(stage: Settings) -> None:
    layout = Layout(stage.data)
    layout.create()
    today = NOW.date()
    old_people = today - timedelta(days=PEOPLE_DAYS + 1)
    old_programs = today - timedelta(days=PROGRAM_DAYS + 1)
    kept_programs = today - timedelta(days=PROGRAM_DAYS)
    names = [
        f"{old_people}.menschen.log.gz",
        f"{old_programs}.automatisch.log.gz",
        f"{old_programs}.menschen.log.gz",
        f"{kept_programs}.automatisch.log.gz",
    ]
    for name in names:
        (layout.days / name).write_bytes(b"")
    layout.remove_expired(today)
    left = sorted(path.name for path in layout.days.iterdir())
    assert left == [
        f"{old_programs}.menschen.log.gz",
        f"{kept_programs}.automatisch.log.gz",
    ]


def test_a_full_disk_stops_the_run(tmp_path: Path) -> None:
    config = settings(tmp_path, min_free_mb=10**12)
    write_log(config.logs, [])
    with pytest.raises(NotEnoughSpaceError, match="MB needed"):
        run(config, NOW, Geodata.load)


def test_geodata_answers_country_and_datacenter(tmp_path: Path) -> None:
    install_geo(tmp_path)
    loaded = Geodata.load(tmp_path)
    assert loaded.country(READER) == "AT"
    assert loaded.country("192.0.2.1") == "??"
    assert loaded.is_datacenter(CLOUD)
    assert not loaded.is_datacenter(READER)


# --- geodata job ---------------------------------------------------------------------

MARKER = b"\xab\xcd\xefMaxMind.com"


def _gz(content: bytes) -> bytes:
    return gzip.compress(content)


def country_csv(rows: int = 100_000) -> bytes:
    """Consecutive /24 ranges from 20.0.0.0 on, plus one IPv6 range."""
    lines: list[str] = []
    for number in range(rows):
        network = f"{20 + number // 65536}.{number // 256 % 256}.{number % 256}"
        lines.append(f"{network}.0,{network}.255,AT")
    lines.append("2a02:8388::,2a02:8388:ffff:ffff:ffff:ffff:ffff:ffff,AT")
    return ("\n".join(lines) + "\n").encode()


def asn_csv() -> bytes:
    """A few known networks before plain filler ranges, IPv6 last as in DB-IP."""
    known = [
        '1.0.0.0,1.0.0.255,13335,"Cloudflare, Inc."',
        '2.28.0.0,2.29.255.255,24940,"Hetzner Online GmbH"',
        '3.0.0.0,3.0.0.255,999001,"Example Hosting Ltd"',
    ]
    filler = [
        f'100.{n // 256}.{n % 256}.0,100.{n // 256}.{n % 256}.255,1,"x"'
        for n in range(40000)
    ]
    six = (
        '2a01:4f8::,2a01:4f8:ffff:ffff:ffff:ffff:ffff:ffff,24940,"Hetzner Online GmbH"'
    )
    return ("\n".join([*known, *filler, six]) + "\n").encode()


def mmdb() -> bytes:
    return b"\0" * (1024 * 1024) + MARKER + b"\0" * 64


def fake_downloads(month: str) -> dict[str, bytes]:
    files = {
        ("country", "csv"): country_csv(),
        ("country", "mmdb"): mmdb(),
        ("asn", "csv"): asn_csv(),
        ("asn", "mmdb"): mmdb(),
    }
    return {
        f"{geodata.BASE_URL}dbip-{name}-lite-{month}.{kind}.gz": _gz(content)
        for (name, kind), content in files.items()
    }


def fetcher_for(urls: dict[str, bytes]) -> Callable[[str], bytes]:
    def fetch_fake(url: str) -> bytes:
        if url not in urls:
            reason = "404"
            raise URLError(reason)
        return urls[url]

    return fetch_fake


def test_the_lists_of_this_month_are_installed(tmp_path: Path) -> None:
    month = update(tmp_path, date(2026, 11, 3), fetcher_for(fake_downloads("2026-11")))
    assert month == "2026-11"
    assert (tmp_path / "VERSION").read_text() == "2026-11\n"
    lookup = AddressLookup(tmp_path / "country-v4.bin", tmp_path / "country-v6.bin")
    assert lookup.lookup("20.0.1.7") == "AT"
    assert lookup.lookup("2a02:8388::1") == "AT"
    datacenters = AddressLookup(
        tmp_path / "datacenter-v4.bin", tmp_path / "datacenter-v6.bin"
    )
    assert datacenters.lookup("2.28.1.1") == "DC"
    assert datacenters.lookup("2a01:4f8::1") == "DC"
    assert datacenters.lookup("1.0.0.1") is None
    assert datacenters.lookup("3.0.0.9") == "DC"
    assert datacenters.lookup("100.0.0.1") is None
    assert (tmp_path / "country.mmdb").read_bytes().endswith(MARKER + b"\0" * 64)


def test_last_months_lists_stand_in(tmp_path: Path) -> None:
    month = update(tmp_path, date(2026, 12, 1), fetcher_for(fake_downloads("2026-11")))
    assert month == "2026-11"


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "not-gzip",
        "too-small",
        "no-mmdb-marker",
        "few-rows",
        "bad-address",
        "mixed-families",
    ],
)
def test_unusable_lists_change_nothing(tmp_path: Path, damage: str) -> None:
    urls = fake_downloads("2026-11")
    key = f"{geodata.BASE_URL}dbip-country-lite-2026-11.csv.gz"
    damaged = {
        "not-gzip": b"plain",
        "too-small": _gz(b"1.0.0.0,1.0.0.255,AU\n"),
        "few-rows": _gz(country_csv()[: 2 * 1024 * 1024].rsplit(b"\n", 1)[0] + b"\n"),
        "bad-address": _gz(b"x,y,AT\n" * 200_000),
        "mixed-families": _gz(b"1.0.0.0,::1,AT\n" * 100_000),
    }
    if damage == "missing":
        del urls[key]
    elif damage == "no-mmdb-marker":
        urls[f"{geodata.BASE_URL}dbip-asn-lite-2026-11.mmdb.gz"] = _gz(
            b"\0" * (2 * 1024 * 1024)
        )
    else:
        urls[key] = damaged[damage]
    (tmp_path / "VERSION").write_text("2026-10\n")
    with pytest.raises(GeodataError, match="could be fetched"):
        update(tmp_path, date(2026, 11, 3), fetcher_for(urls))
    assert (tmp_path / "VERSION").read_text() == "2026-10\n"
    assert not (tmp_path / "country-v4.bin").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["VERSION"]


def test_downloads_must_use_https() -> None:
    with pytest.raises(GeodataError, match="non-HTTPS"):
        fetch("http://download.db-ip.com/free/x.gz")


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, limit: int) -> bytes:
        return self._body[:limit]

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_downloads_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(geodata, "LARGEST_FILE", 10)
    body = b"x" * 11

    def answer(_request: object, timeout: int) -> _Response:
        assert timeout == geodata.TIMEOUT_SECONDS
        return _Response(body)

    monkeypatch.setattr(geodata, "urlopen", answer)
    with pytest.raises(GeodataError, match="larger than 10 bytes"):
        fetch("https://download.db-ip.com/free/x.gz")
    body = b"ok"
    assert fetch("https://download.db-ip.com/free/x.gz") == b"ok"


# --- command line --------------------------------------------------------------


def arguments(config: Settings, *extra: str) -> list[str]:
    return [
        "run",
        "--logs",
        str(config.logs),
        "--data",
        str(config.data),
        "--www",
        str(config.www),
        "--site",
        str(config.site),
        "--host",
        "AugustinerKirche.at",
        "--host",
        "hochamt.at",
        *extra,
    ]


def test_the_command_line_runs_the_hourly_job(stage: Settings) -> None:
    assert cli.main(arguments(stage)) == 0
    assert (stage.www / "index.html").is_file()


def test_the_command_line_reports_a_full_disk(stage: Settings) -> None:
    assert cli.main(arguments(stage, "--min-free-mb", str(10**12))) == cli.EXIT_FAILED


def test_the_command_line_runs_the_geodata_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []

    def record(directory: Path, _today: date) -> str:
        calls.append(directory)
        return "2026-11"

    monkeypatch.setattr(cli, "update", record)
    assert cli.main(["geodata", "--data", str(tmp_path)]) == 0
    assert calls == [tmp_path / "geo"]

    def fail(_directory: Path, _today: date) -> str:
        reason = "nothing"
        raise GeodataError(reason)

    monkeypatch.setattr(cli, "update", fail)
    assert cli.main(["geodata", "--data", str(tmp_path)]) == cli.EXIT_FAILED


def test_a_run_without_a_live_log_reads_the_rolled_parts(tmp_path: Path) -> None:
    config = settings(tmp_path)
    config.logs.mkdir(parents=True)
    with gzip.open(
        config.logs / "access-2026-11-25T10-00-00.000.log.gz", "wt"
    ) as stream:
        stream.write("\n".join(reading("2026-11-25 09:00:00")) + "\n")
    run(config, NOW, Geodata.load)
    assert Layout(config.data).finished_days() == {date(2026, 11, 25)}
