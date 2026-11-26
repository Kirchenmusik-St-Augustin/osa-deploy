"""The log viewer (Dozzle): its own Caddy site block and its domain check."""

from pathlib import Path

import pytest

from osa_tools.playbooks import REPOSITORY_ROOT
from tests.ansible_helpers import OSA_DOMAINS, render_caddyfile, run_deploy_tasks

CADDYFILE_TEMPLATE = REPOSITORY_ROOT / "config" / "caddy" / "Caddyfile.j2"
DOZZLE_QUADLET = REPOSITORY_ROOT / "quadlets" / "logging" / "logging-dozzle.container"
LOGGING_DOMAIN = OSA_DOMAINS["logging_domain"]
DOZZLE_UPSTREAM = "reverse_proxy 127.0.0.1:8081"

DOMAIN_TASKS = {
    "Check the domain of the log viewer",
    "Refuse placeholder or malformed inventory values",
}
DOMAIN_RULE = "logging_domain must name the log viewer on a host name of its own"
HOST_RULE = "is not a usable public hostname"


def _site_block(rendered: str, domain: str) -> str:
    start = rendered.index(f"\n{domain} {{\n")
    end = rendered.index("\n}\n", start)
    return rendered[start:end]


@pytest.mark.parametrize(
    "domains",
    [{}, {"frontend_domain": "www.osa-tests.org"}],
    ids=["shared-application-domain", "split-application-domains"],
)
def test_only_the_log_viewer_block_reaches_dozzle(
    tmp_path: Path, domains: dict[str, str]
) -> None:
    rendered = render_caddyfile(tmp_path, CADDYFILE_TEMPLATE, **domains)
    block = _site_block(rendered, LOGGING_DOMAIN)
    assert rendered.count(DOZZLE_UPSTREAM) == 1
    assert DOZZLE_UPSTREAM in block
    assert "/logging/dozzle" not in rendered
    assert "import security_headers" in block
    assert "import access_log" in block
    assert "{$LOGGING_USER} {$LOGGING_PASSWORD_HASH}" in block
    # Nothing is proxied before the credentials are checked.
    assert block.index("basic_auth") < block.index(DOZZLE_UPSTREAM)
    assert "remote_ip" not in block


def test_listed_networks_are_refused_before_the_credentials(tmp_path: Path) -> None:
    rendered = render_caddyfile(
        tmp_path,
        CADDYFILE_TEMPLATE,
        logging_allowed_cidrs=["203.0.113.0/24", "2001:db8::/32"],
    )
    block = _site_block(rendered, LOGGING_DOMAIN)
    assert "@outside_allowlist not remote_ip 203.0.113.0/24 2001:db8::/32" in block
    assert block.index("respond @outside_allowlist 403") < block.index("basic_auth")


def test_dozzle_serves_from_the_root_of_its_domain() -> None:
    assert "DOZZLE_BASE" not in DOZZLE_QUADLET.read_text(encoding="utf-8")


def test_a_log_viewer_name_of_its_own_passes(tmp_path: Path) -> None:
    result = run_deploy_tasks(
        tmp_path,
        DOMAIN_TASKS,
        augustiner_domain="church-tests.org",
        augustiner_alias_domains=["www.church-tests.org"],
        augustiner_redirects={"church-tests.at": "/pages/a.html"},
    )
    assert result.returncode == 0, result.stdout


def test_a_stage_without_a_log_viewer_name_is_refused(tmp_path: Path) -> None:
    result = run_deploy_tasks(tmp_path, DOMAIN_TASKS, {"logging_domain"})
    assert result.returncode != 0, result.stdout
    assert DOMAIN_RULE in result.stdout, result.stdout


@pytest.mark.parametrize(
    ("site", "reason"),
    [
        ({"logging_domain": ["logs.osa-tests.org"]}, DOMAIN_RULE),
        ({"logging_domain": OSA_DOMAINS["backend_domain"]}, DOMAIN_RULE),
        ({"logging_domain": OSA_DOMAINS["shorturl_domain"]}, DOMAIN_RULE),
        (
            {
                "augustiner_domain": "church-tests.org",
                "logging_domain": "church-tests.org",
            },
            DOMAIN_RULE,
        ),
        (
            {
                "augustiner_domain": "church-tests.org",
                "augustiner_alias_domains": ["www.church-tests.org"],
                "logging_domain": "www.church-tests.org",
            },
            DOMAIN_RULE,
        ),
        (
            {
                "augustiner_domain": "church-tests.org",
                "augustiner_redirects": {"church-tests.at": "/pages/a.html"},
                "logging_domain": "church-tests.at",
            },
            DOMAIN_RULE,
        ),
        ({"logging_domain": "CHANGEME.example.invalid"}, HOST_RULE),
    ],
    ids=[
        "not-a-string",
        "same-as-backend",
        "same-as-shorturl",
        "same-as-website",
        "same-as-website-alias",
        "same-as-website-redirect",
        "placeholder",
    ],
)
def test_an_unusable_log_viewer_name_stops_the_deploy(
    tmp_path: Path, site: dict[str, object], reason: str
) -> None:
    result = run_deploy_tasks(tmp_path, DOMAIN_TASKS, **site)
    assert result.returncode != 0, result.stdout
    assert reason in result.stdout, result.stdout
