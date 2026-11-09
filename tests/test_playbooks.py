import json
from pathlib import Path

from osa_tools.playbooks import PLAYBOOK_DIR, build_argv
from osa_tools.site import Site
from osa_tools.workspace import Session

SESSION = Session(password_file=Path("/pw"), key_file=Path("/key"))


def _site(tmp_path: Path) -> Site:
    return Site(name="test", directory=tmp_path / "test")


def test_argv_carries_inventory_site_and_credentials(tmp_path: Path) -> None:
    site = _site(tmp_path)
    argv = build_argv("deploy.yml", site, SESSION, [], dry_run=False)
    assert argv[0] == "ansible-playbook"
    assert argv[argv.index("-i") + 1] == str(site.inventory)
    assert json.loads(argv[argv.index("-e") + 1]) == {
        "osa_site_dir": str(site.directory)
    }
    assert argv[argv.index("--vault-password-file") + 1] == "/pw"
    assert argv[argv.index("--private-key") + 1] == "/key"
    assert str(PLAYBOOK_DIR / "deploy.yml") in argv
    assert "--check" not in argv


def test_dry_run_adds_check_and_diff(tmp_path: Path) -> None:
    argv = build_argv("deploy.yml", _site(tmp_path), SESSION, [], dry_run=True)
    assert argv[-2:] == ["--check", "--diff"]


def test_extra_arguments_come_last_and_unchanged(tmp_path: Path) -> None:
    argv = build_argv(
        "setup_vps.yml",
        _site(tmp_path),
        SESSION,
        ["-u", "root", "--tags", "x"],
        dry_run=True,
    )
    assert argv[-4:] == ["-u", "root", "--tags", "x"]
    assert argv.index("--diff") < argv.index("-u")
