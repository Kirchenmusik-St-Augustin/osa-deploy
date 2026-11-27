"""The visitor statistics in the Caddyfile, in caddy.container and in deploy.yml.

The Caddyfile and the checks run through Ansible itself, so the tests see
exactly what a deploy renders and asserts.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
import yaml

from osa_tools.playbooks import PLAYBOOK_DIR, REPOSITORY_ROOT
from tests.ansible_helpers import render_template, run_deploy_tasks

CADDYFILE_TEMPLATE = REPOSITORY_ROOT / "config" / "caddy" / "Caddyfile.j2"
CADDY_QUADLET = REPOSITORY_ROOT / "quadlets" / "caddy" / "caddy.container"
FILTER_TEMPLATE = REPOSITORY_ROOT / "templates" / "augustiner-stats-filter.container.j2"
STATS_MARKER = "{% if augustiner_stats_domain is defined %}"

SITE: dict[str, object] = {
    "augustiner_domain": "church-tests.org",
    "augustiner_alias_domains": ["www.church-tests.org"],
    "augustiner_redirects": {
        "church-tests.at": "/",
        "church-tests.eu": "/pages/a.html",
    },
}
STATS = "stats.church-tests.org"


def _block(rendered: str, address: str) -> str:
    """The site block that starts with the given address line."""
    start = rendered.index(f"\n{address} {{\n")
    end = rendered.index("\n}\n", start)
    return rendered[start : end + 2]


def _unit_values(text: str, key: str) -> list[str]:
    prefix = f"{key}="
    return [
        line.removeprefix(prefix)
        for line in text.splitlines()
        if line.startswith(prefix)
    ]


@pytest.fixture
def rendered(tmp_path: Path) -> str:
    return render_template(
        tmp_path, CADDYFILE_TEMPLATE, augustiner_stats_domain=STATS, **SITE
    )


# --- Caddyfile ---------------------------------------------------------------


def test_without_statistics_the_caddyfile_stays_as_before(tmp_path: Path) -> None:
    text = CADDYFILE_TEMPLATE.read_text(encoding="utf-8")
    stripped = re.sub(
        re.escape(STATS_MARKER) + r"\n.*?\{% endif %\}\n", "", text, flags=re.DOTALL
    )
    assert STATS_MARKER not in stripped
    without_statistics = tmp_path / "without-statistics.j2"
    without_statistics.write_text(stripped, encoding="utf-8")
    rendered = render_template(tmp_path, CADDYFILE_TEMPLATE, **SITE)
    assert rendered == render_template(tmp_path, without_statistics, **SITE)
    assert "augustiner" in rendered
    assert "augustiner_log" not in rendered
    assert "augustiner-stats" not in rendered


def test_every_name_of_the_website_also_logs_to_the_statistics(rendered: str) -> None:
    for address in (
        "church-tests.org",
        "www.church-tests.org",
        "church-tests.at",
        "church-tests.eu",
    ):
        block = _block(rendered, address)
        assert "\timport access_log\n" in block, address
        assert "\timport augustiner_log\n" in block, address


def test_other_sites_keep_only_their_usual_log(rendered: str) -> None:
    for address in (
        "app.osa-tests.org",
        "go.osa-tests.org",
        "logs.osa-tests.org",
        STATS,
    ):
        block = _block(rendered, address)
        assert "\timport access_log\n" in block, address
        assert "augustiner_log" not in block, address


def test_the_raw_log_rolls_small_and_is_gone_after_seven_days(rendered: str) -> None:
    snippet = rendered[rendered.index("(augustiner_log) {") :]
    snippet = snippet[: snippet.index("\n}\n")]
    assert "output file /var/log/augustiner/access.log {" in snippet
    assert "roll_size 1MiB" in snippet
    assert "roll_keep_for 168h" in snippet
    (keep,) = re.findall(r"roll_keep (\d+)", snippet)
    # Rolling at 1 MiB, the files of a week must all fit.
    assert int(keep) >= 5000


def test_the_statistics_share_the_login_of_the_log_viewer(rendered: str) -> None:
    login = "basic_auth {\n\t\t\t{$LOGGING_USER} {$LOGGING_PASSWORD_HASH}\n\t\t}"
    assert login in _block(rendered, "logs.osa-tests.org")
    assert login in _block(rendered, STATS)


def test_the_statistics_ask_for_the_login_before_anything_else(rendered: str) -> None:
    block = _block(rendered, STATS)
    route = block[block.index("\troute {") :]
    assert route.index("basic_auth") < route.index("respond @not_read 405")
    assert route.index("basic_auth") < route.index("file_server")
    assert "root * /srv/augustiner-stats" in route
    assert "@not_read not method GET HEAD" in route


def test_only_the_goaccess_reports_may_run_scripts(rendered: str) -> None:
    block = _block(rendered, STATS)
    policies = dict(
        re.findall(r'header @(\w+) Content-Security-Policy "([^"]*)"', block)
    )
    assert set(policies) == {"overview", "detail"}
    assert "script-src" not in policies["overview"]
    assert "script-src 'self' 'unsafe-inline' 'unsafe-eval'" in policies["detail"]
    for policy in policies.values():
        assert policy.startswith("default-src 'none';")
        assert "frame-ancestors 'none'" in policy
    assert "@detail path /menschen.html /automatisch.html" in block
    assert "@overview not path /menschen.html /automatisch.html" in block


def test_error_answers_keep_the_security_headers(rendered: str) -> None:
    """A 401 or 404 would otherwise go out without HSTS and with Server."""
    block = _block(rendered, STATS)
    errors = block[block.index("\thandle_errors {") :]
    assert "import augustiner_stats_headers" in errors
    assert "Content-Security-Policy \"default-src 'none'" in errors
    headers = rendered[rendered.index("(augustiner_stats_headers) {") :]
    headers = headers[: headers.index("\n}\n")]
    for header in (
        'Strict-Transport-Security "max-age=31536000"',
        'X-Robots-Tag "noindex, nofollow, noarchive"',
        'Cache-Control "no-store"',
        'Referrer-Policy "no-referrer"',
        "-Server",
    ):
        assert header in headers


# --- Caddy and the jobs share two directories --------------------------------


def test_caddy_writes_the_raw_log_and_only_reads_the_pages(tmp_path: Path) -> None:
    caddy = _unit_values(CADDY_QUADLET.read_text(encoding="utf-8"), "Volume")
    assert "%h/data/augustiner-logs:/var/log/augustiner:rw,z" in caddy
    assert "%h/data/augustiner-stats/www:/srv/augustiner-stats:ro,z" in caddy
    stats_filter = _unit_values(
        render_template(tmp_path, FILTER_TEMPLATE, **SITE), "Volume"
    )
    assert "%h/data/augustiner-logs:/logs:ro,z" in stats_filter
    assert "%h/data/augustiner-stats:/data:rw,z" in stats_filter


# --- deploy.yml --------------------------------------------------------------

DOMAIN_TASKS = {
    "Check the domain of the visitor statistics",
    "Refuse placeholder or malformed inventory values",
}


def test_a_statistics_name_of_its_own_passes(tmp_path: Path) -> None:
    result = run_deploy_tasks(
        tmp_path, DOMAIN_TASKS, augustiner_stats_domain=STATS, **SITE
    )
    assert result.returncode == 0, result.stdout


def test_a_stage_without_statistics_passes(tmp_path: Path) -> None:
    result = run_deploy_tasks(tmp_path, DOMAIN_TASKS, **SITE)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("site", "message"),
    [
        ({"augustiner_stats_domain": STATS}, "augustiner_stats_domain needs"),
        ({**SITE, "augustiner_stats_domain": "church-tests.org"}, "needs"),
        ({**SITE, "augustiner_stats_domain": "www.church-tests.org"}, "needs"),
        ({**SITE, "augustiner_stats_domain": "church-tests.eu"}, "needs"),
        ({**SITE, "augustiner_stats_domain": "logs.osa-tests.org"}, "needs"),
        ({**SITE, "augustiner_stats_domain": "app.osa-tests.org"}, "needs"),
        ({**SITE, "augustiner_stats_domain": ["a.org"]}, "needs"),
        (
            {**SITE, "augustiner_stats_domain": "stats.example.org"},
            "is not a usable public hostname",
        ),
        (
            {**SITE, "augustiner_stats_domain": "Stats_Church.org"},
            "is not a usable public hostname",
        ),
    ],
    ids=[
        "without-site",
        "main-domain",
        "alias",
        "redirect",
        "log-viewer",
        "application",
        "not-a-string",
        "placeholder",
        "malformed",
    ],
)
def test_an_unusable_statistics_name_stops_the_deploy(
    tmp_path: Path, site: dict[str, object], message: str
) -> None:
    result = run_deploy_tasks(tmp_path, DOMAIN_TASKS, **site)
    assert result.returncode != 0
    assert message in result.stdout


def _tasks() -> list[Mapping[str, object]]:
    plays = yaml.safe_load((PLAYBOOK_DIR / "deploy.yml").read_text(encoding="utf-8"))
    return plays[0]["tasks"]


def _task(name: str) -> Mapping[str, object]:
    (task,) = (task for task in _tasks() if task["name"] == name)
    return task


def _vars() -> Mapping[str, object]:
    plays = yaml.safe_load((PLAYBOOK_DIR / "deploy.yml").read_text(encoding="utf-8"))
    return plays[0]["vars"]


def test_the_statistics_units_and_directories_reach_every_stage() -> None:
    variables = _vars()
    targets = cast("list[Mapping[str, str]]", variables["quadlet_targets"])
    assert {"src": "augustiner-stats", "dest": "augustiner-stats"} in targets
    loop = cast("list[str]", _task("Create the directory structure")["loop"])
    for directory in (
        "{{ augustiner_logs_dir }}",
        "{{ augustiner_stats_dir }}",
        "{{ augustiner_stats_dir }}/www",
    ):
        assert directory in loop
    assert variables["augustiner_logs_dir"] == (
        "{{ ansible_user_dir }}/data/augustiner-logs"
    )
    assert variables["augustiner_stats_dir"] == (
        "{{ ansible_user_dir }}/data/augustiner-stats"
    )


def test_the_filter_is_rendered_only_where_the_statistics_run() -> None:
    rendering = _task(
        "Render the filter Quadlet of the visitor statistics"
        " (names of the website from the inventory)"
    )
    assert rendering["when"] == "augustiner_stats_domain is defined"
    module = cast("Mapping[str, str]", rendering["ansible.builtin.template"])
    assert module["src"].endswith("/augustiner-stats-filter.container.j2")
    removal = _task("Remove the filter Quadlet where the visitor statistics are off")
    assert removal["when"] == "augustiner_stats_domain is not defined"
    removed = cast("Mapping[str, str]", removal["ansible.builtin.file"])
    assert removed["path"] == module["dest"]
    assert removed["state"] == "absent"


def test_the_filter_is_in_place_before_systemd_reloads() -> None:
    names = [task["name"] for task in _tasks()]
    reload = names.index("systemctl --user daemon-reload")
    assert (
        names.index(
            "Render the filter Quadlet of the visitor statistics"
            " (names of the website from the inventory)"
        )
        < reload
    )


def test_deploy_runs_the_timers_only_where_the_inventory_names_the_statistics() -> None:
    assert _vars()["augustiner_stats_timers"] == [
        "augustiner-stats.timer",
        "augustiner-stats-geodata.timer",
    ]
    task = _task("Enable or disable the timers of the visitor statistics")
    assert task["loop"] == "{{ augustiner_stats_timers }}"
    module = cast("Mapping[str, str]", task["ansible.builtin.systemd"])
    assert module["enabled"] == "{{ augustiner_stats_domain is defined }}"
    assert "stopped" in str(module["state"])


def test_the_first_deploy_fills_the_statistics_at_once() -> None:
    geodata = _task("Fetch the geodata of the visitor statistics on their first deploy")
    report = _task("Render the visitor statistics on their first deploy")
    for task, unit, marker in (
        (geodata, "augustiner-stats-geodata.service", "results[0]"),
        (report, "augustiner-stats-report.service", "results[1]"),
    ):
        condition = str(task["when"])
        assert "augustiner_stats_domain is defined" in condition
        assert "not ansible_check_mode" in condition
        assert f"not augustiner_stats_files.{marker}.stat.exists" in condition
        module = cast("Mapping[str, str]", task["ansible.builtin.systemd"])
        assert module["name"] == unit
    looked_up = _task(
        "Look for the geodata and the dashboard of the visitor statistics"
    )
    assert looked_up["loop"] == [
        "{{ augustiner_stats_dir }}/geo/VERSION",
        "{{ augustiner_stats_dir }}/www/index.html",
    ]


def test_a_failed_geodata_download_only_warns() -> None:
    """DB-IP being down must not stop a deploy halfway, before Caddy restarts."""
    geodata = _task("Fetch the geodata of the visitor statistics on their first deploy")
    assert geodata["ignore_errors"] is True
    warning = _task("Warn when the first geodata download failed")
    assert warning["when"] == "augustiner_geodata_first_run is failed"
    report = _task("Render the visitor statistics on their first deploy")
    assert "ignore_errors" not in report


def test_the_first_runs_come_after_the_website_is_cloned() -> None:
    names = [task["name"] for task in _tasks()]
    clone = names.index("Clone the static website on its first deploy")
    assert clone < names.index(
        "Fetch the geodata of the visitor statistics on their first deploy"
    )
    assert clone < names.index("Render the visitor statistics on their first deploy")
