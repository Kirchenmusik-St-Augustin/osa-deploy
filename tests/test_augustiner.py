"""The optional static website: mirror script, Caddy blocks and domain checks.

The mirror script runs against a local bare repository with the git of the
test host; the Caddyfile and the domain checks run through Ansible itself, so
the tests see exactly what a deploy renders and asserts.
"""

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
import yaml

from osa_tools.playbooks import PLAYBOOK_DIR, REPOSITORY_ROOT
from tests.ansible_helpers import render_caddyfile, run_deploy_tasks

PULL_SCRIPT = REPOSITORY_ROOT / "quadlets" / "augustiner" / "pull.sh"
PULL_QUADLET = REPOSITORY_ROOT / "quadlets" / "augustiner" / "augustiner-pull.container"
PULL_KNOWN_HOSTS = REPOSITORY_ROOT / "quadlets" / "augustiner" / "known_hosts"
PULL_TIMER = REPOSITORY_ROOT / "systemd-timers" / "augustiner-pull.timer"
CADDY_QUADLET = REPOSITORY_ROOT / "quadlets" / "caddy" / "caddy.container"
CADDYFILE_TEMPLATE = REPOSITORY_ROOT / "config" / "caddy" / "Caddyfile.j2"
SITE_MARKER = "{% if augustiner_domain is defined %}"

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
    """A bare repository standing in for GitHub, plus a working copy to push from.

    The deploy key and the host keys are placeholders: git reads a local path
    without SSH, so only their presence matters here.
    """

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.env = _git_env(root)
        self.bare = root / "upstream.git"
        self.work = root / "author"
        self.ssh_key = root / "deploy.key"
        self.ssh_key.write_text("placeholder key\n", encoding="utf-8")
        self.known_hosts = root / "known_hosts"
        self.known_hosts.write_text("placeholder host key\n", encoding="utf-8")
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


def pull_env(upstream: Upstream, target: Path, min_free_mb: int = 1) -> dict[str, str]:
    return {
        **upstream.env,
        "AUG_REPO_URL": str(upstream.bare),
        "AUG_TARGET": str(target),
        "AUG_MIN_FREE_MB": str(min_free_mb),
        "AUG_SSH_KEY": str(upstream.ssh_key),
        "AUG_KNOWN_HOSTS": str(upstream.known_hosts),
    }


def run_script(env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(PULL_SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def run_pull(
    upstream: Upstream, target: Path, min_free_mb: int = 1
) -> subprocess.CompletedProcess[str]:
    return run_script(pull_env(upstream, target, min_free_mb))


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


@pytest.mark.parametrize(
    "missing",
    ["AUG_REPO_URL", "AUG_TARGET", "AUG_MIN_FREE_MB", "AUG_SSH_KEY", "AUG_KNOWN_HOSTS"],
)
def test_the_script_refuses_to_run_without_its_settings(
    upstream: Upstream, target: Path, missing: str
) -> None:
    env = pull_env(upstream, target)
    del env[missing]
    result = run_script(env)
    assert result.returncode != 0
    assert f"{missing} is not set" in result.stderr


@pytest.mark.parametrize("unreadable", ["AUG_SSH_KEY", "AUG_KNOWN_HOSTS"])
def test_nothing_runs_without_the_key_files(
    upstream: Upstream, target: Path, unreadable: str
) -> None:
    upstream.commit({"index.html": "v1"}, "first")
    env = pull_env(upstream, target)
    env[unreadable] = str(target.parent / "absent")
    result = run_script(env)
    assert result.returncode == 1
    assert f"cannot read {target.parent / 'absent'}" in result.stderr
    assert not target.exists()


def test_a_moved_repository_is_followed(
    upstream: Upstream, target: Path, tmp_path: Path
) -> None:
    upstream.commit({"index.html": "old home", "old.html": "old"}, "first")
    run_pull(upstream, target)
    moved = Upstream(tmp_path / "moved")
    new_head = moved.commit({"index.html": "new home"}, "unrelated history")
    result = run_script({**pull_env(upstream, target), "AUG_REPO_URL": str(moved.bare)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("updated ")
    assert head_of(target) == new_head
    assert (target / "index.html").read_text(encoding="utf-8") == "new home"
    assert not (target / "old.html").exists()
    origin = subprocess.run(
        ["git", "-C", str(target), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert origin.stdout.strip() == str(moved.bare)


def test_ssh_uses_only_the_deploy_key_and_the_pinned_host_keys(
    upstream: Upstream, target: Path, tmp_path: Path
) -> None:
    """A stand-in ssh records how git calls it, then refuses the connection."""
    tools = tmp_path / "bin"
    tools.mkdir()
    recorded = tmp_path / "ssh-args"
    fake_ssh = tools / "ssh"
    fake_ssh.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{recorded}"\nexit 255\n', encoding="utf-8"
    )
    fake_ssh.chmod(0o755)
    env = {
        **pull_env(upstream, target),
        "AUG_REPO_URL": "git@github.com:owner/website.git",
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
    }
    result = run_script(env)
    assert result.returncode != 0
    assert not target.exists()
    arguments = recorded.read_text(encoding="utf-8").splitlines()
    assert arguments[arguments.index("-i") + 1] == str(upstream.ssh_key)
    for option in (
        "IdentitiesOnly=yes",
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        f"UserKnownHostsFile={upstream.known_hosts}",
    ):
        assert option in arguments
    assert "git@github.com" in arguments


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


def test_the_mirror_fetches_over_ssh_with_read_only_key_files() -> None:
    (url,) = _unit_values(PULL_QUADLET, "Environment=AUG_REPO_URL")
    assert url.startswith("git@github.com:")
    environment = _unit_values(PULL_QUADLET, "Environment")
    assert "AUG_SSH_KEY=/opt/augustiner/deploy.key" in environment
    assert "AUG_KNOWN_HOSTS=/opt/augustiner/known_hosts" in environment
    mounts = _unit_values(PULL_QUADLET, "Volume")
    assert "%h/.env/augustiner-pull.key:/opt/augustiner/deploy.key:ro,z" in mounts
    assert (
        "%h/.config/containers/systemd/augustiner/known_hosts:"
        "/opt/augustiner/known_hosts:ro,z"
    ) in mounts


def test_only_the_host_keys_of_github_are_trusted() -> None:
    entries = [
        line.split()
        for line in PULL_KNOWN_HOSTS.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    assert {host for host, _type, _key in entries} == {"github.com"}
    assert {key_type for _host, key_type, _key in entries} == {
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ssh-rsa",
    }


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
    for application_snippet in ("frontend_csp", "backend_api", "127.0.0.1:8081"):
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


def test_each_redirect_target_gets_one_temporary_block(tmp_path: Path) -> None:
    rendered = render_caddyfile(
        tmp_path,
        CADDYFILE_TEMPLATE,
        augustiner_domain="church-tests.org",
        augustiner_redirects={
            "church-tests.at": "/pages/a.html",
            "church-tests.net": "/",
            "www.church-tests.at": "/pages/a.html",
            "www.church-tests.net": "/",
            "church-tests.eu": "/pages/a.html#p-x",
        },
    )
    page = _site_block(rendered, "church-tests.at, www.church-tests.at")
    assert "\tredir https://church-tests.org/pages/a.html 302\n" in page + "\n"
    home = _site_block(rendered, "church-tests.net, www.church-tests.net")
    assert "\tredir https://church-tests.org/ 302\n" in home + "\n"
    # The anchor stays part of the target, so this name gets its own block.
    anchor = _site_block(rendered, "church-tests.eu")
    assert "\tredir https://church-tests.org/pages/a.html#p-x 302\n" in anchor + "\n"
    for block in (page, home, anchor):
        assert "import access_log" in block
        assert "header -Server" in block
        assert "{uri}" not in block
        assert "file_server" not in block
    # Three targets, three blocks; the main domain itself is no redirect.
    assert rendered.count("redir https://church-tests.org/") == 3
    assert "\nchurch-tests.org {\n" in rendered


def test_without_redirects_the_site_renders_as_before(tmp_path: Path) -> None:
    site: dict[str, object] = {
        "augustiner_domain": "church-tests.org",
        "augustiner_alias_domains": ["www.church-tests.org"],
    }
    before = render_caddyfile(tmp_path, CADDYFILE_TEMPLATE, **site)
    assert before == render_caddyfile(
        tmp_path, CADDYFILE_TEMPLATE, **site, augustiner_redirects={}
    )
    assert "redir https://church-tests.org/" not in before


DOMAIN_TASKS = {
    "Check the domains of the static website",
    "Refuse placeholder or malformed inventory values",
    "Require the deploy key of the static website",
}


def secrets_dir(tmp_path: Path, *names: str) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    for name in names:
        (directory / name).write_text("$ANSIBLE_VAULT;1.1;AES256\n", encoding="utf-8")
    return directory


def run_domain_checks(
    tmp_path: Path, **site: object
) -> subprocess.CompletedProcess[str]:
    """Runs the site checks of deploy.yml for a stage that has the deploy key."""
    secrets = secrets_dir(tmp_path, "augustiner-pull.key")
    return run_deploy_tasks(
        tmp_path, DOMAIN_TASKS, local_secrets_path=str(secrets), **site
    )


REDIRECTS = {
    "church-tests.at": "/pages/a.html",
    "www.church-tests.at": "/pages/a.html",
    "church-tests.net": "/",
    "www.church-tests.net": "/",
    "church-tests.eu": "/pages/a.html#p-berufung",
}

DOMAIN_RULES = "augustiner_alias_domains and augustiner_redirects need"
HOST_RULES = "is not a usable public hostname"


@pytest.mark.parametrize(
    "site",
    [
        {},
        {"augustiner_domain": "www.church-tests.org"},
        {
            "augustiner_domain": "www.church-tests.org",
            "augustiner_alias_domains": ["church-tests.org", "www.church-tests.at"],
        },
        {
            "augustiner_domain": "church-tests.org",
            "augustiner_alias_domains": ["www.church-tests.org"],
            "augustiner_redirects": REDIRECTS,
        },
    ],
    ids=["no-site", "main-domain", "aliases", "aliases-and-redirects"],
)
def test_valid_domains_pass(tmp_path: Path, site: dict[str, object]) -> None:
    result = run_domain_checks(tmp_path, **site)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("site", "reason"),
    [
        ({"augustiner_alias_domains": ["church-tests.org"]}, DOMAIN_RULES),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_alias_domains": "church-tests.org",
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_alias_domains": ["church-tests.org", "church-tests.org"],
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_alias_domains": ["www.church-tests.org"],
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_alias_domains": ["go.osa-tests.org"],
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_alias_domains": ["Bad_Name"],
            },
            HOST_RULES,
        ),
        ({"augustiner_domain": "changeme.church-tests.org"}, HOST_RULES),
        ({"augustiner_redirects": {"church-tests.at": "/"}}, DOMAIN_RULES),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_redirects": ["church-tests.at"],
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_redirects": {"www.church-tests.org": "/"},
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_alias_domains": ["church-tests.at"],
                "augustiner_redirects": {"church-tests.at": "/"},
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_redirects": {"go.osa-tests.org": "/"},
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_redirects": {"church-tests.at": "pages/a.html"},
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_redirects": {"church-tests.at": "/a b {uri}"},
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_redirects": {"church-tests.at": 302},
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_redirects": {"church-tests.at": "/a.html#p x"},
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_redirects": {"church-tests.at": "/a.html#"},
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_redirects": {"church-tests.at": "/a.html#b#c"},
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_redirects": {"church-tests.at": "#p-x"},
            },
            DOMAIN_RULES,
        ),
        (
            {
                "augustiner_domain": "www.church-tests.org",
                "augustiner_redirects": {"Bad_Name": "/"},
            },
            HOST_RULES,
        ),
    ],
    ids=[
        "alias-without-main-domain",
        "alias-list-as-string",
        "duplicate-alias",
        "main-domain-as-alias",
        "osa-domain-as-alias",
        "malformed-alias",
        "placeholder-main-domain",
        "redirect-without-main-domain",
        "redirects-as-list",
        "main-domain-as-redirect",
        "alias-as-redirect",
        "osa-domain-as-redirect",
        "relative-target",
        "target-with-caddy-syntax",
        "target-not-a-string",
        "anchor-with-space",
        "empty-anchor",
        "two-anchors",
        "anchor-without-path",
        "malformed-redirect-host",
    ],
)
def test_invalid_domains_stop_the_deploy(
    tmp_path: Path, site: dict[str, object], reason: str
) -> None:
    result = run_domain_checks(tmp_path, **site)
    assert result.returncode != 0, result.stdout
    assert reason in result.stdout, result.stdout


DEPLOY_KEY_RULE = "secrets/augustiner-pull.key is missing"


def test_a_stage_with_the_site_needs_the_deploy_key(tmp_path: Path) -> None:
    result = run_deploy_tasks(
        tmp_path,
        DOMAIN_TASKS,
        local_secrets_path=str(secrets_dir(tmp_path)),
        augustiner_domain="www.church-tests.org",
    )
    assert result.returncode != 0, result.stdout
    assert DEPLOY_KEY_RULE in result.stdout, result.stdout


def test_a_stage_without_the_site_needs_no_deploy_key(tmp_path: Path) -> None:
    result = run_deploy_tasks(
        tmp_path, DOMAIN_TASKS, local_secrets_path=str(secrets_dir(tmp_path))
    )
    assert result.returncode == 0, result.stdout


SECRET_TASKS = {
    "Find static *.env and *.key files in the secrets directory of the stage",
    "Find templated *.env.j2 files in the secrets directory of the stage",
    "Refuse to deploy secret files that are not vault-encrypted",
}


def test_a_plain_text_deploy_key_is_refused(tmp_path: Path) -> None:
    secrets = secrets_dir(tmp_path, "caddy.env")
    (secrets / "augustiner-pull.key").write_text(
        "plain text instead of vault\n", encoding="utf-8"
    )
    result = run_deploy_tasks(tmp_path, SECRET_TASKS, local_secrets_path=str(secrets))
    assert result.returncode != 0, result.stdout
    assert "augustiner-pull.key" in result.stdout, result.stdout


def test_an_encrypted_deploy_key_passes(tmp_path: Path) -> None:
    secrets = secrets_dir(tmp_path, "caddy.env", "augustiner-pull.key")
    result = run_deploy_tasks(tmp_path, SECRET_TASKS, local_secrets_path=str(secrets))
    assert result.returncode == 0, result.stdout
