import stat
from pathlib import Path

import pytest

from osa_tools import commands, vault
from osa_tools.errors import OsaError
from osa_tools.site import Site
from tests.conftest import PASSWORD, make_reader


class _Recorder:
    def __init__(self, result: int = 0) -> None:
        self.result = result
        self.calls: list[tuple[list[str], Path | None]] = []

    def __call__(self, argv: list[str], cwd: Path | None = None) -> int:
        self.calls.append((list(argv), cwd))
        return self.result


@pytest.fixture
def real_site(site: Site) -> Site:
    """The stage fixture with a genuinely encrypted operator key."""
    password_file = site.directory / "pw"
    password_file.write_text(PASSWORD + "\n", encoding="utf-8")
    key = site.directory / "key"
    key.write_text("PRIVATE-KEY-MATERIAL\n", encoding="utf-8")
    vault.encrypt_to(key, password_file, site.operator_key_vault)
    key.unlink()
    password_file.unlink()
    return site


def test_session_unpacks_a_private_key_and_cleans_up(real_site: Site) -> None:
    with commands.open_session(real_site, make_reader(PASSWORD)) as session:
        assert session.key_file.read_text(encoding="utf-8") == "PRIVATE-KEY-MATERIAL\n"
        assert stat.S_IMODE(session.key_file.stat().st_mode) == 0o600
        assert session.password_file.read_text(encoding="utf-8") == PASSWORD + "\n"
    assert not session.key_file.exists()
    assert not session.password_file.exists()


def test_session_with_a_wrong_password(real_site: Site) -> None:
    with (
        pytest.raises(OsaError, match="wrong vault password"),
        commands.open_session(real_site, make_reader("x" * 25)),
    ):
        pass


def test_run_playbook_runs_ansible_from_the_repository_root(
    real_site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder(result=4)
    monkeypatch.setattr(commands, "run", recorder)
    result = commands.run_playbook(
        real_site,
        "deploy.yml",
        ["--tags", "deploy-backend"],
        make_reader(PASSWORD),
        dry_run=False,
    )
    assert result == 4
    argv, cwd = recorder.calls[0]
    assert argv[0] == "ansible-playbook"
    assert argv[-2:] == ["--tags", "deploy-backend"]
    assert cwd == commands.REPOSITORY_ROOT


def test_open_ssh_uses_the_operator_key_and_the_inventory_host(
    real_site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(commands, "run", recorder)
    commands.open_ssh(real_site, ["-v"], make_reader(PASSWORD))
    argv, _ = recorder.calls[0]
    assert argv[0] == "ssh"
    assert "IdentitiesOnly=yes" in argv
    assert argv[-2:] == ["-v", "service@host.example.org"]
    assert argv[argv.index("-i") + 1].endswith("operator_key")


def test_vault_action_runs_in_the_stage_directory(
    real_site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder(result=0)
    monkeypatch.setattr(vault, "run", recorder)
    commands.run_vault(real_site, "edit", ["secrets/x"], make_reader(PASSWORD))
    argv, cwd = recorder.calls[0]
    assert argv[:2] == ["ansible-vault", "edit"]
    assert cwd == real_site.directory


def test_rekey_reencrypts_every_vaulted_file_of_the_stage(real_site: Site) -> None:
    new_password = "a-completely-new-password-9"
    result = commands.run_vault(
        real_site,
        "rekey",
        [],
        make_reader(PASSWORD, new_password, new_password),
    )
    assert result == 0
    with commands.open_session(real_site, make_reader(new_password)) as session:
        assert session.key_file.exists()


def test_rekey_refuses_different_repetitions(real_site: Site) -> None:
    with pytest.raises(OsaError, match="differ"):
        commands.run_vault(
            real_site, "rekey", [], make_reader(PASSWORD, "n" * 25, "m" * 25)
        )


def test_rekey_refuses_a_short_new_password(real_site: Site) -> None:
    with pytest.raises(OsaError, match="at least 20"):
        commands.run_vault(real_site, "rekey", [], make_reader(PASSWORD, "short"))


def test_init_creates_a_complete_stage(tmp_path: Path) -> None:
    sites = tmp_path / "sites"
    sites.mkdir()
    created = commands.init_site(sites, "qa", None, make_reader(PASSWORD, PASSWORD))
    assert created == sites / "qa"
    inventory = (created / "inventory.ini").read_text(encoding="utf-8")
    assert "app_environment=qa" in inventory
    assert "__STAGE__" not in inventory
    assert (created / "secrets" / "osa-backend-worker.env.example").is_file()
    public_key = (created / "operator_keys.pub").read_text(encoding="utf-8")
    assert public_key.startswith("ssh-ed25519 ")
    assert "osa-operator-qa" in public_key
    site = Site(name="qa", directory=created)
    with commands.open_session(site, make_reader(PASSWORD)) as session:
        assert "BEGIN OPENSSH PRIVATE KEY" in session.key_file.read_text(
            encoding="utf-8"
        )


def test_init_accepts_a_different_directory_name(tmp_path: Path) -> None:
    sites = tmp_path / "sites"
    sites.mkdir()
    created = commands.init_site(
        sites, "production-new", "production", make_reader(PASSWORD, PASSWORD)
    )
    inventory = (created / "inventory.ini").read_text(encoding="utf-8")
    assert "app_environment=production" in inventory


def test_init_refuses_an_unknown_environment(tmp_path: Path) -> None:
    with pytest.raises(OsaError, match="not a deploy environment"):
        commands.init_site(tmp_path, "staging", None, make_reader())


def test_init_refuses_a_missing_sites_directory(tmp_path: Path) -> None:
    with pytest.raises(OsaError, match="does not exist"):
        commands.init_site(tmp_path / "none", "qa", None, make_reader())


def test_init_refuses_an_existing_stage(tmp_path: Path) -> None:
    (tmp_path / "qa").mkdir()
    with pytest.raises(OsaError, match="already exists"):
        commands.init_site(tmp_path, "qa", None, make_reader())


def test_init_refuses_a_short_password(tmp_path: Path) -> None:
    with pytest.raises(OsaError, match="at least 20"):
        commands.init_site(tmp_path, "qa", None, make_reader("short"))
    assert not (tmp_path / "qa").exists()


def test_init_refuses_different_repetitions(tmp_path: Path) -> None:
    with pytest.raises(OsaError, match="differ"):
        commands.init_site(tmp_path, "qa", None, make_reader(PASSWORD, "x" * 25))
    assert not (tmp_path / "qa").exists()
