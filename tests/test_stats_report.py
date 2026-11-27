"""The GoAccess report script, the statistics units and their timers.

report.sh runs with the shell of the test host against a stand-in for
GoAccess that records every call, so the tests see which days reach which
database, in which order and with which options. The real GoAccess was checked
by hand in the pinned image; the units are checked as files and, for the
filter, as Ansible renders them.
"""

import gzip
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from augustiner_stats.days import goaccess_line
from augustiner_stats.pipeline import PEOPLE, PEOPLE_DAYS, PROGRAM_DAYS, PROGRAMS, QUEUE
from osa_tools.playbooks import REPOSITORY_ROOT
from tests.ansible_helpers import render_template
from tests.stats_helpers import request

STATS_DIR = REPOSITORY_ROOT / "quadlets" / "augustiner-stats"
REPORT_SCRIPT = STATS_DIR / "report.sh"
PANEL_SCRIPT = STATS_DIR / "automatisch.js"
REPORT_QUADLET = STATS_DIR / "augustiner-stats-report.container"
GEODATA_QUADLET = STATS_DIR / "augustiner-stats-geodata.container"
FILTER_TEMPLATE = REPOSITORY_ROOT / "templates" / "augustiner-stats-filter.container.j2"
TIMERS = REPOSITORY_ROOT / "systemd-timers"

STUB = f"""#!{sys.executable}
import json, os, sys
arguments = sys.argv[1:]
call = {{"args": arguments, "stdin": sys.stdin.read() if "-" in arguments else None}}
with open(os.environ["STUB_CALLS"], "a", encoding="utf-8") as calls:
    calls.write(json.dumps(call) + "\\n")
if os.environ.get("STUB_FAIL", "\\0") in " ".join(arguments):
    sys.exit(1)
for argument in arguments:
    if argument.startswith("--output="):
        with open(argument.removeprefix("--output="), "w", encoding="utf-8") as out:
            out.write("<html>" + " ".join(arguments) + "</html>")
"""


@dataclass(frozen=True)
class Call:
    args: list[str]
    stdin: str | None

    @property
    def report(self) -> str:
        (database,) = (a for a in self.args if a.startswith("--db-path="))
        return Path(database).name

    @property
    def stores(self) -> bool:
        return "--persist" in self.args


class Stats:
    """A data directory, the GoAccess stand-in and a way to run the script."""

    def __init__(self, root: Path) -> None:
        self.data = root / "data"
        self.www = self.data / "www"
        self.calls_file = root / "calls.jsonl"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "goaccess"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STATS_DATA": str(self.data),
            "STATS_ASSETS": str(STATS_DIR),
            "STUB_CALLS": str(self.calls_file),
        }
        for directory in ("state", "days", "current", "geo"):
            (self.data / directory).mkdir(parents=True)

    def close_day(
        self, day: str, people: Sequence[str], programs: Sequence[str]
    ) -> None:
        for report, lines in ((PEOPLE, people), (PROGRAMS, programs)):
            with gzip.open(self.data / "days" / f"{day}.{report}.log.gz", "wt") as out:
                out.writelines(f"{line}\n" for line in lines)

    def queue(self, *days: str) -> None:
        with (self.data / "state" / QUEUE).open("a", encoding="utf-8") as queue:
            queue.writelines(f"{day}\n" for day in days)

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        self.calls_file.unlink(missing_ok=True)
        return subprocess.run(
            ["sh", str(REPORT_SCRIPT)],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self) -> list[Call]:
        if not self.calls_file.is_file():
            return []
        return [
            Call(**json.loads(line))
            for line in self.calls_file.read_text(encoding="utf-8").splitlines()
        ]

    def stored(self) -> list[tuple[str, str]]:
        """(report, first line) of every call that adds to a database."""
        return [
            (call.report, (call.stdin or "").split("\n", 1)[0])
            for call in self.calls()
            if call.stores
        ]


@pytest.fixture
def stats(tmp_path: Path) -> Stats:
    return Stats(tmp_path)


def _option(call: Call, name: str) -> list[str]:
    return [a.removeprefix(f"{name}=") for a in call.args if a.startswith(f"{name}=")]


# --- adding closed days ----------------------------------------------------


def test_a_first_run_without_any_data_renders_both_reports(stats: Stats) -> None:
    result = stats.run()
    assert result.returncode == 0, result.stderr
    calls = stats.calls()
    assert [call.report for call in calls] == [PEOPLE, PROGRAMS]
    assert not any(call.stores for call in calls)
    assert all("/dev/null" in call.args for call in calls)
    assert sorted(path.name for path in stats.www.iterdir()) == [
        "automatisch.html",
        "automatisch.js",
        "menschen.html",
    ]


def test_queued_days_are_added_once_oldest_first(stats: Stats) -> None:
    stats.close_day("2026-11-24", ["people 24"], ["programs 24"])
    stats.close_day("2026-11-25", ["people 25"], ["programs 25"])
    stats.queue("2026-11-25", "2026-11-24", "2026-11-25")
    result = stats.run()
    assert result.returncode == 0, result.stderr
    assert stats.stored() == [
        (PEOPLE, "people 24"),
        (PROGRAMS, "programs 24"),
        (PEOPLE, "people 25"),
        (PROGRAMS, "programs 25"),
    ]
    assert all("--process-and-exit" in c.args for c in stats.calls() if c.stores)
    assert sorted(path.name for path in (stats.data / "state").iterdir()) == []
    assert stats.run().returncode == 0
    assert stats.stored() == []


def test_a_day_queued_again_is_not_counted_twice(stats: Stats) -> None:
    """GoAccess would add the same lines a second time."""
    stats.close_day("2026-11-24", ["people 24"], ["programs 24"])
    stats.close_day("2026-11-25", ["people 25"], ["programs 25"])
    stats.queue("2026-11-25")
    assert stats.run().returncode == 0
    stats.queue("2026-11-24", "2026-11-25")
    result = stats.run()
    assert result.returncode == 0
    assert stats.stored() == []
    assert "menschen already holds 2026-11-24 or a later day" in result.stdout


def test_a_day_without_lines_of_one_report_still_adds_the_other(stats: Stats) -> None:
    stats.close_day("2026-11-24", ["people 24"], [])
    (stats.data / "days" / "2026-11-24.automatisch.log.gz").unlink()
    stats.queue("2026-11-24")
    result = stats.run()
    assert result.returncode == 0, result.stderr
    assert stats.stored() == [(PEOPLE, "people 24")]
    assert "no automatisch lines kept for 2026-11-24" in result.stdout


def test_a_failed_run_keeps_its_days_and_repeats_only_the_missing_ones(
    stats: Stats,
) -> None:
    stats.close_day("2026-11-24", ["people 24"], ["programs 24"])
    stats.close_day("2026-11-25", ["people 25"], ["programs 25"])
    stats.queue("2026-11-24", "2026-11-25")
    old_report = "<html>yesterday</html>"
    stats.www.mkdir()
    (stats.www / "menschen.html").write_text(old_report, encoding="utf-8")
    failed = stats.run(STUB_FAIL=f"--process-and-exit --keep-last={PROGRAM_DAYS}")
    assert failed.returncode != 0
    assert stats.stored() == [(PEOPLE, "people 24"), (PROGRAMS, "programs 24")]
    assert (stats.www / "menschen.html").read_text(encoding="utf-8") == old_report
    stats.queue("2026-11-26")
    stats.close_day("2026-11-26", ["people 26"], ["programs 26"])
    assert stats.run().returncode == 0
    assert stats.stored() == [
        (PROGRAMS, "programs 24"),
        (PEOPLE, "people 25"),
        (PROGRAMS, "programs 25"),
        (PEOPLE, "people 26"),
        (PROGRAMS, "programs 26"),
    ]


def test_a_damaged_day_stops_the_run_before_goaccess_sees_it(stats: Stats) -> None:
    (stats.data / "days" / "2026-11-24.menschen.log.gz").write_bytes(b"not gzip")
    stats.queue("2026-11-24")
    result = stats.run()
    assert result.returncode != 0
    assert "damaged file" in result.stderr
    assert stats.calls() == []


@pytest.mark.parametrize("entry", ["../../etc/passwd", "2026-11-24 extra", "yesterday"])
def test_an_unexpected_queue_entry_is_refused(stats: Stats, entry: str) -> None:
    stats.queue(entry)
    result = stats.run()
    assert result.returncode != 0
    assert "unexpected entry in the queue" in result.stderr
    assert stats.calls() == []


def test_the_script_refuses_to_run_without_its_settings(stats: Stats) -> None:
    for name in ("STATS_DATA", "STATS_ASSETS"):
        env = {key: value for key, value in stats.env.items() if key != name}
        result = subprocess.run(
            ["sh", str(REPORT_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert f"{name} is not set" in result.stderr


# --- rendering ---------------------------------------------------------------


def test_the_reports_add_the_current_day_without_storing_it(stats: Stats) -> None:
    for report in (PEOPLE, PROGRAMS):
        (stats.data / "current" / f"{report}.log").write_text(
            "today\n", encoding="utf-8"
        )
    assert stats.run().returncode == 0
    for call in stats.calls():
        assert str(stats.data / "current" / f"{call.report}.log") in call.args
        assert "--restore" in call.args
        assert not call.stores
        (output,) = _option(call, "--output")
        assert Path(output).parent == stats.data / "goaccess"
    assert not list((stats.data / "goaccess").glob("*.html"))


def test_people_and_programs_are_kept_as_long_as_the_filter_keeps_them(
    stats: Stats,
) -> None:
    assert stats.run().returncode == 0
    people, programs = stats.calls()
    assert _option(people, "--keep-last") == [str(PEOPLE_DAYS)]
    assert _option(programs, "--keep-last") == [str(PROGRAM_DAYS)]


def test_the_people_report_hides_addresses_and_drops_known_crawlers(
    stats: Stats,
) -> None:
    assert stats.run().returncode == 0
    people, programs = stats.calls()
    assert "--ignore-crawlers" in people.args
    assert _option(people, "--ignore-panel") == ["REMOTE_USER", "HOSTS"]
    assert _option(people, "--html-report-title") == ["Besucher im Detail"]
    assert "--ignore-crawlers" not in programs.args
    assert _option(programs, "--ignore-panel") == ["HOSTS"]
    assert _option(programs, "--html-custom-js") == ["automatisch.js"]
    assert _option(programs, "--html-report-title") == ["Automatische Zugriffe"]
    assert all("--no-global-config" in call.args for call in (people, programs))


def test_the_geodata_databases_are_used_only_when_present(stats: Stats) -> None:
    assert stats.run().returncode == 0
    assert all(not _option(call, "--geoip-database") for call in stats.calls())
    for database in ("country", "asn"):
        (stats.data / "geo" / f"{database}.mmdb").write_bytes(b"mmdb")
    assert stats.run().returncode == 0
    for call in stats.calls():
        assert _option(call, "--geoip-database") == [
            str(stats.data / "geo" / "country.mmdb"),
            str(stats.data / "geo" / "asn.mmdb"),
        ]


def test_the_panel_script_is_published_next_to_the_report(stats: Stats) -> None:
    assert stats.run().returncode == 0
    published = (stats.www / "automatisch.js").read_text(encoding="utf-8")
    assert published == PANEL_SCRIPT.read_text(encoding="utf-8")
    assert '"Art des Zugriffs"' in published
    assert "window.user_interface" in published


def _format_pattern(log_format: str) -> re.Pattern[str]:
    fields = {
        "%v": r"\S+",
        "%h": r"\S+",
        "%e": r"\S+",
        "%d": r"\d{4}-\d{2}-\d{2}",
        "%t": r"\d{2}:\d{2}:\d{2}",
        "%r": r"[^\"]+",
        "%s": r"\d{3}",
        "%b": r"\d+",
        "%R": r"[^\"]*",
        "%u": r"[^\"]*",
        "%D": r"\d+",
    }
    parts = re.split(r"(%[a-zA-Z])", log_format)
    return re.compile(
        "".join(fields[part] if part in fields else re.escape(part) for part in parts)
    )


def test_goaccess_reads_the_lines_in_the_format_the_filter_writes() -> None:
    (log_format,) = re.findall(
        r"^log_format='(.*)'$",
        REPORT_SCRIPT.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    line = goaccess_line(
        request(
            "2026-11-26 10:00:00",
            "/pages/musik.html",
            query="utm_source=pfarrblatt",
            referer="https://www.google.com/search?q=augustinerkirche",
        ),
        "203.0.113.0-ab12",
        "suchmaschine",
        "Firefox/133 (Linux)",
    )
    assert _format_pattern(log_format).fullmatch(line), line


# --- units -------------------------------------------------------------------


def _unit_values(text: str, key: str) -> list[str]:
    prefix = f"{key}="
    return [
        line.removeprefix(prefix)
        for line in text.splitlines()
        if line.startswith(prefix)
    ]


def _filter_unit(tmp_path: Path, **site: object) -> str:
    return render_template(
        tmp_path, FILTER_TEMPLATE, augustiner_domain="augustinerkirche.at", **site
    )


@pytest.fixture
def filter_unit(tmp_path: Path) -> str:
    return _filter_unit(tmp_path)


def _units(filter_unit: str) -> Mapping[str, str]:
    return {
        "filter": filter_unit,
        "report": REPORT_QUADLET.read_text(encoding="utf-8"),
        "geodata": GEODATA_QUADLET.read_text(encoding="utf-8"),
    }


def test_every_statistics_container_is_pinned_and_hardened(filter_unit: str) -> None:
    for name, text in _units(filter_unit).items():
        (image,) = _unit_values(text, "Image")
        assert "@sha256:" in image, name
        assert _unit_values(text, "ReadOnly") == ["true"], name
        assert _unit_values(text, "NoNewPrivileges") == ["true"], name
        assert _unit_values(text, "DropCapability") == ["all"], name
        assert _unit_values(text, "Mount") == [
            "type=tmpfs,target=/tmp,tmpfs-mode=1777,noexec"
        ], name
        assert _unit_values(text, "PidsLimit") == ["64"], name
        assert _unit_values(text, "Type") == ["oneshot"], name
        assert "[Install]" not in text.splitlines(), name


@pytest.mark.parametrize(
    ("name", "memory"), [("filter", "256m"), ("report", "384m"), ("geodata", "256m")]
)
def test_every_statistics_container_has_a_memory_limit(
    filter_unit: str, name: str, memory: str
) -> None:
    text = _units(filter_unit)[name]
    assert _unit_values(text, "PodmanArgs") == [f"--memory={memory}"]


def test_only_the_geodata_job_reaches_the_network(filter_unit: str) -> None:
    units = _units(filter_unit)
    assert _unit_values(units["filter"], "Network") == ["none"]
    assert _unit_values(units["report"], "Network") == ["none"]
    assert _unit_values(units["geodata"], "Network") == []
    assert "network-online.target" in _unit_values(units["geodata"], "After")


def test_the_filter_reads_the_raw_log_and_the_site_only(filter_unit: str) -> None:
    assert _unit_values(filter_unit, "Volume") == [
        "%h/data/augustiner-logs:/logs:ro,z",
        "%h/data/augustiner/site:/site:ro,z",
        "%h/data/augustiner-stats:/data:rw,z",
        "%h/.config/containers/systemd/augustiner-stats:/opt/augustiner-stats:ro,z",
    ]
    for name, text in _units(filter_unit).items():
        if name != "filter":
            assert not any("augustiner-logs" in v for v in _unit_values(text, "Volume"))


def test_the_filter_knows_every_name_of_the_website(tmp_path: Path) -> None:
    unit = _filter_unit(
        tmp_path,
        augustiner_alias_domains=["www.augustinerkirche.at"],
        augustiner_redirects={
            "augustiner.wien": "https://augustinerkirche.at/",
            "www.augustiner.wien": "https://augustinerkirche.at/",
        },
    )
    (command,) = _unit_values(unit, "Exec")
    assert command.startswith("-E -s -m augustiner_stats run ")
    assert "--www /data/www" in command
    assert re.findall(r"--host (\S+)", command) == [
        "augustinerkirche.at",
        "www.augustinerkirche.at",
        "augustiner.wien",
        "www.augustiner.wien",
    ]
    assert _unit_values(unit, "WorkingDir") == ["/opt/augustiner-stats"]


def test_the_filter_needs_no_aliases_or_redirects(filter_unit: str) -> None:
    (command,) = _unit_values(filter_unit, "Exec")
    assert re.findall(r"--host (\S+)", command) == ["augustinerkirche.at"]


def test_the_report_runs_after_the_filter_in_german() -> None:
    text = REPORT_QUADLET.read_text(encoding="utf-8")
    assert _unit_values(text, "Requires") == ["augustiner-stats-filter.service"]
    assert _unit_values(text, "After") == ["augustiner-stats-filter.service"]
    assert _unit_values(text, "Exec") == ["/opt/augustiner-stats/report.sh"]
    environment = _unit_values(text, "Environment")
    for setting in (
        "LANG=de_DE.UTF-8",
        "LC_ALL=de_DE.UTF-8",
        "LANGUAGE=de",
        "TZ=Europe/Vienna",
        "STATS_DATA=/data",
        "STATS_ASSETS=/opt/augustiner-stats",
    ):
        assert setting in environment


def test_the_hourly_timer_starts_the_report_after_the_full_hour() -> None:
    text = (TIMERS / "augustiner-stats.timer").read_text(encoding="utf-8")
    assert _unit_values(text, "OnCalendar") == ["*:05"]
    assert _unit_values(text, "Unit") == ["augustiner-stats-report.service"]


def test_the_geodata_timer_runs_monthly_and_catches_up() -> None:
    text = (TIMERS / "augustiner-stats-geodata.timer").read_text(encoding="utf-8")
    assert _unit_values(text, "OnCalendar") == ["*-*-03 04:30:00"]
    assert _unit_values(text, "Persistent") == ["true"]
    assert _unit_values(text, "Unit") == ["augustiner-stats-geodata.service"]
