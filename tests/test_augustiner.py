"""The optional static website: mirror script, Caddy blocks and domain checks.

The mirror script runs against a local bare repository with the git of the
test host; the Caddyfile and the domain checks run through Ansible itself, so
the tests see exactly what a deploy renders and asserts.
"""

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
import yaml

from osa_tools.playbooks import PLAYBOOK_DIR, REPOSITORY_ROOT

PULL_SCRIPT = REPOSITORY_ROOT / "quadlets" / "augustiner" / "pull.sh"
PULL_QUADLET = REPOSITORY_ROOT / "quadlets" / "augustiner" / "augustiner-pull.container"
PULL_TIMER = REPOSITORY_ROOT / "systemd-timers" / "augustiner-pull.timer"
CADDY_QUADLET = REPOSITORY_ROOT / "quadlets" / "caddy" / "caddy.container"
CADDYFILE_TEMPLATE = REPOSITORY_ROOT / "config" / "caddy" / "Caddyfile.j2"
SITE_MARKER = "{% if augustiner_domain is defined %}"

OSA_DOMAINS = {
    "backend_domain": "app.osa-tests.org",
    "frontend_domain": "app.osa-tests.org",
    "shorturl_domain": "go.osa-tests.org",
    "acme_email": "ops@osa-tests.org",
}


# --- mirror script ---------------------------------------------------------


def _git_env(home: Path) -> dict[str, str]:
    """Isolates git from the configuration of the machine running the tests."""
    return {
        **os.environ,
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.org",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.org",
    }


class Upstream:
    """A bare repository standing in for GitHub, plus a working copy to push from."""

    def __init__(self, root: Path) -> None:
        self.env = _git_env(root)
        self.bare = root / "upstream.git"
        self.work = root / "author"
        self._git(
            root, "init", "--quiet", "--bare", "--initial-branch=main", str(self.bare)
        )
        self._git(root, "clone", "--quiet", str(self.bare), str(self.work))
        self._git(self.work, "checkout", "--quiet", "-b", "main")

    def _git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def commit(self, files: Mapping[str, str], message: str) -> str:
        for name, content in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self._git(self.work, "add", "--all")
        self._git(self.work, "commit", "--quiet", "-m", message)
        self._git(self.work, "push", "--quiet", "--force", "origin", "main")
        return self._git(self.work, "rev-parse", "HEAD")

    def commit_symlink(self, name: str, target: str) -> None:
        (self.work / name).symlink_to(target)
        self._git(self.work, "add", "--all")
        self._git(self.work, "commit", "--quiet", "-m", "link")
        self._git(self.work, "push", "--quiet", "origin", "main")

    def rewrite_history(self, files: Mapping[str, str]) -> str:
        """Replaces the whole branch with one new commit, like a force-push."""
        self._git(self.work, "checkout", "--quiet", "--orphan", "rewritten")
        self._git(self.work, "rm", "--quiet", "-r", "--cached", ".")
        for path in self.work.iterdir():
            if path.name != ".git":
                path.unlink()
        self._git(self.work, "branch", "--quiet", "-D", "main")
        self._git(self.work, "checkout", "--quiet", "-b", "main")
        return self.commit(files, "rewritten")


@pytest.fixture
def upstream(tmp_path: Path) -> Upstream:
    return Upstream(tmp_path)


@pytest.fixture
def target(tmp_path: Path) -> Path:
    parent = tmp_path / "data"
    parent.mkdir()
    return parent / "site"


def run_pull(
    upstream: Upstream, target: Path, min_free_mb: int = 1
) -> subprocess.CompletedProcess[str]:
    env = {
        **upstream.env,
        "AUG_REPO_URL": str(upstream.bare),
        "AUG_TARGET": str(target),
        "AUG_MIN_FREE_MB": str(min_free_mb),
    }
    return subprocess.run(
        ["sh", str(PULL_SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def head_of(repository: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_first_run_clones_without_symlink_support(
    upstream: Upstream, target: Path
) -> None:
    first = upstream.commit({"index.html": "v1"}, "first")
    result = run_pull(upstream, target)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("cloned ")
    assert (target / "index.html").read_text(encoding="utf-8") == "v1"
    assert head_of(target) == first
    assert not target.with_name("site.tmp").exists()
    symlinks = subprocess.run(
        ["git", "-C", str(target), "config", "core.symlinks"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert symlinks.stdout.strip() == "false"


def test_later_runs_follow_new_commits_and_report_no_change(
    upstream: Upstream, target: Path
) -> None:
    upstream.commit({"index.html": "v1"}, "first")
    run_pull(upstream, target)
    second = upstream.commit({"index.html": "v2", "pages/new.html": "new"}, "second")
    updated = run_pull(upstream, target)
    assert updated.returncode == 0, updated.stderr
    assert updated.stdout.startswith("updated ")
    assert head_of(target) == second
    assert (target / "pages" / "new.html").exists()
    unchanged = run_pull(upstream, target)
    assert unchanged.returncode == 0, unchanged.stderr
    assert unchanged.stdout.startswith("unchanged ")


def test_a_force_push_upstream_is_followed(upstream: Upstream, target: Path) -> None:
    upstream.commit({"index.html": "v1", "old.html": "old"}, "first")
    run_pull(upstream, target)
    rewritten = upstream.rewrite_history({"index.html": "rewritten"})
    result = run_pull(upstream, target)
    assert result.returncode == 0, result.stderr
    assert head_of(target) == rewritten
    assert (target / "index.html").read_text(encoding="utf-8") == "rewritten"
    assert not (target / "old.html").exists()


def test_an_update_with_a_symlink_is_refused_and_leaves_the_copy_alone(
    upstream: Upstream, target: Path
) -> None:
    first = upstream.commit({"index.html": "v1"}, "first")
    run_pull(upstream, target)
    upstream.commit_symlink("key.txt", "/data/caddy/certificates")
    result = run_pull(upstream, target)
    assert result.returncode == 1
    assert "symbolic links: key.txt" in result.stderr
    assert head_of(target) == first
    assert not (target / "key.txt").exists()
    assert not (target / "key.txt").is_symlink()


def test_a_first_clone_with_a_symlink_is_refused(
    upstream: Upstream, target: Path
) -> None:
    upstream.commit({"index.html": "v1"}, "first")
    upstream.commit_symlink("key.txt", "/data/caddy/certificates")
    result = run_pull(upstream, target)
    assert result.returncode == 1
    assert "symbolic links: key.txt" in result.stderr
    assert not target.exists()


def test_nothing_runs_when_the_disk_is_nearly_full(
    upstream: Upstream, target: Path
) -> None:
    upstream.commit({"index.html": "v1"}, "first")
    result = run_pull(upstream, target, min_free_mb=10**9)
    assert result.returncode == 1
    assert "at least 1000000000 MB required" in result.stderr
    assert not target.exists()
    assert not target.with_name("site.tmp").exists()


@pytest.mark.parametrize("missing", ["AUG_REPO_URL", "AUG_TARGET", "AUG_MIN_FREE_MB"])
def test_the_script_refuses_to_run_without_its_settings(
    upstream: Upstream, target: Path, missing: str
) -> None:
    env = {
        **upstream.env,
        "AUG_REPO_URL": str(upstream.bare),
        "AUG_TARGET": str(target),
        "AUG_MIN_FREE_MB": "1",
    }
    del env[missing]
    result = subprocess.run(
        ["sh", str(PULL_SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert f"{missing} is not set" in result.stderr


# --- units -----------------------------------------------------------------


def _unit_values(path: Path, key: str) -> list[str]:
    prefix = f"{key}="
    return [
        line.removeprefix(prefix)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(prefix)
    ]


def test_the_mirror_container_is_pinned_and_hardened() -> None:
    (image,) = _unit_values(PULL_QUADLET, "Image")
    assert "@sha256:" in image
    assert _unit_values(PULL_QUADLET, "ReadOnly") == ["true"]
    assert _unit_values(PULL_QUADLET, "NoNewPrivileges") == ["true"]
    assert _unit_values(PULL_QUADLET, "DropCapability") == ["all"]
    assert _unit_values(PULL_QUADLET, "Type") == ["oneshot"]
    assert "[Install]" not in PULL_QUADLET.read_text(encoding="utf-8").splitlines()


def test_only_the_mirror_writes_and_caddy_only_reads_the_copy() -> None:
    mirror_mounts = _unit_values(PULL_QUADLET, "Volume")
    assert "%h/data/augustiner:/work:rw,z" in mirror_mounts
    assert "%h/data/augustiner:/srv/augustiner:ro,z" in _unit_values(
        CADDY_QUADLET, "Volume"
    )


def test_the_timer_runs_every_five_minutes() -> None:
    assert _unit_values(PULL_TIMER, "OnCalendar") == ["*:0/5"]


def _deploy_task(name: str) -> Mapping[str, object]:
    plays = yaml.safe_load((PLAYBOOK_DIR / "deploy.yml").read_text(encoding="utf-8"))
    tasks: list[Mapping[str, object]] = plays[0]["tasks"]
    (task,) = (task for task in tasks if task["name"] == name)
    return task


def test_deploy_runs_the_timer_only_where_the_inventory_names_the_site() -> None:
    task = _deploy_task("Enable or disable the timer that mirrors the static website")
    module = cast("Mapping[str, str]", task["ansible.builtin.systemd"])
    assert module["name"] == "augustiner-pull.timer"
    assert module["enabled"] == "{{ augustiner_domain is defined }}"
    assert "stopped" in str(module["state"])


def test_deploy_clones_only_on_the_first_real_run() -> None:
    task = _deploy_task("Clone the static website on its first deploy")
    condition = str(task["when"])
    assert "not augustiner_copy.stat.exists" in condition
    assert "not ansible_check_mode" in condition


# --- Caddyfile and domain checks through Ansible ---------------------------


def _ansible_env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp"),
        "ANSIBLE_NOCOLOR": "1",
    }


def render_caddyfile(tmp_path: Path, template: Path, **site: object) -> str:
    destination = tmp_path / "Caddyfile"
    variables = tmp_path / "vars.json"
    variables.write_text(json.dumps({**OSA_DOMAINS, **site}), encoding="utf-8")
    result = subprocess.run(
        [
            "ansible",
            "localhost",
            "-i",
            "localhost,",
            "-c",
            "local",
            "-m",
            "ansible.builtin.template",
            "-a",
            f"src={template} dest={destination}",
            "-e",
            f"@{variables}",
        ],
        env=_ansible_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return destination.read_text(encoding="utf-8")


def test_without_the_site_the_caddyfile_stays_as_before(tmp_path: Path) -> None:
    text = CADDYFILE_TEMPLATE.read_text(encoding="utf-8")
    assert text.count(SITE_MARKER) == 1
    application_only = tmp_path / "application-only.j2"
    application_only.write_text(text.split(SITE_MARKER)[0], encoding="utf-8")
    rendered = render_caddyfile(tmp_path, CADDYFILE_TEMPLATE)
    assert rendered == render_caddyfile(tmp_path, application_only)
    assert "augustiner" not in rendered


def _site_block(rendered: str, domain: str) -> str:
    start = rendered.index(f"\n{domain} {{\n")
    end = rendered.index("\n}\n", start)
    return rendered[start:end]


def test_the_site_block_serves_only_the_live_package(tmp_path: Path) -> None:
    rendered = render_caddyfile(
        tmp_path, CADDYFILE_TEMPLATE, augustiner_domain="www.church-tests.org"
    )
    block = _site_block(rendered, "www.church-tests.org")
    assert "root * /srv/augustiner/site" in block
    assert "import augustiner_headers" in block
    assert 'X-Robots-Tag "noindex, nofollow, noarchive"' in block
    for application_snippet in ("frontend_csp", "backend_api", "logging_viewer"):
        assert application_snippet not in block
    assert "@not_read not method GET HEAD" in block
    assert "@outside_package not path / /index.html" in block
    assert "js/admin" in block
    assert "-verwaltung" in block
    # The allowlist answers before file_server can serve anything.
    assert block.index("respond @outside_package 404") < block.index("file_server")
    assert "redir https://www.church-tests.org" not in rendered


def test_the_final_domain_drops_noindex(tmp_path: Path) -> None:
    rendered = render_caddyfile(
        tmp_path,
        CADDYFILE_TEMPLATE,
        augustiner_domain="www.church-tests.org",
        augustiner_noindex=False,
    )
    assert "X-Robots-Tag" not in rendered


def test_all_aliases_share_one_redirect_block(tmp_path: Path) -> None:
    rendered = render_caddyfile(
        tmp_path,
        CADDYFILE_TEMPLATE,
        augustiner_domain="www.church-tests.org",
        augustiner_alias_domains=[
            "church-tests.org",
            "church-tests.at",
            "www.church-tests.at",
        ],
    )
    block = _site_block(
        rendered, "church-tests.org, church-tests.at, www.church-tests.at"
    )
    assert "redir https://www.church-tests.org{uri} permanent" in block
    assert rendered.count("redir https://www.church-tests.org{uri}") == 1


def run_domain_checks(
    tmp_path: Path, **site: object
) -> subprocess.CompletedProcess[str]:
    """Runs the two domain tasks of deploy.yml on their own against localhost."""
    plays = yaml.safe_load((PLAYBOOK_DIR / "deploy.yml").read_text(encoding="utf-8"))
    tasks: list[Mapping[str, object]] = plays[0]["tasks"]
    wanted = {
        "Check the domains of the static website",
        "Refuse placeholder or malformed inventory values",
    }
    playbook = tmp_path / "domains.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "tasks": [task for task in tasks if task["name"] in wanted],
                }
            ]
        ),
        encoding="utf-8",
    )
    variables = tmp_path / "vars.json"
    variables.write_text(json.dumps({**OSA_DOMAINS, **site}), encoding="utf-8")
    return subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-e", f"@{variables}", str(playbook)],
        env=_ansible_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "site",
    [
        {},
        {"augustiner_domain": "www.church-tests.org"},
        {
            "augustiner_domain": "www.church-tests.org",
            "augustiner_alias_domains": ["church-tests.org", "www.church-tests.at"],
        },
    ],
)
def test_valid_domains_pass(tmp_path: Path, site: dict[str, object]) -> None:
    result = run_domain_checks(tmp_path, **site)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "site",
    [
        {"augustiner_alias_domains": ["church-tests.org"]},
        {
            "augustiner_domain": "www.church-tests.org",
            "augustiner_alias_domains": "church-tests.org",
        },
        {
            "augustiner_domain": "www.church-tests.org",
            "augustiner_alias_domains": ["church-tests.org", "church-tests.org"],
        },
        {
            "augustiner_domain": "www.church-tests.org",
            "augustiner_alias_domains": ["www.church-tests.org"],
        },
        {
            "augustiner_domain": "www.church-tests.org",
            "augustiner_alias_domains": ["go.osa-tests.org"],
        },
        {
            "augustiner_domain": "www.church-tests.org",
            "augustiner_alias_domains": ["Bad_Name"],
        },
        {"augustiner_domain": "changeme.church-tests.org"},
    ],
    ids=[
        "alias-without-main-domain",
        "alias-list-as-string",
        "duplicate-alias",
        "main-domain-as-alias",
        "osa-domain-as-alias",
        "malformed-alias",
        "placeholder-main-domain",
    ],
)
def test_invalid_domains_stop_the_deploy(
    tmp_path: Path, site: dict[str, object]
) -> None:
    result = run_domain_checks(tmp_path, **site)
    assert result.returncode != 0, result.stdout
