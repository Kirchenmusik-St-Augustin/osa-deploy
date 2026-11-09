from pathlib import Path

import pytest

from osa_tools import cli, commands
from osa_tools.site import Site
from tests.conftest import make_reader


def test_playbook_commands_pass_the_extra_arguments_through(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, list[str], bool]] = []

    def fake(
        _site: Site,
        playbook: str,
        extra: list[str],
        _reader: object,
        *,
        dry_run: bool,
    ) -> int:
        seen.append((playbook, extra, dry_run))
        return 0

    monkeypatch.setattr(commands, "run_playbook", fake)
    sites = str(site.directory.parent)
    cli.main(["--sites-dir", sites, "setup", "test", "-u", "root", "--ask-pass"])
    cli.main(["--sites-dir", sites, "deploy", "test", "--tags", "deploy-backend"])
    cli.main(["--sites-dir", sites, "check", "test"])
    assert seen == [
        ("setup_vps.yml", ["-u", "root", "--ask-pass"], False),
        ("deploy.yml", ["--tags", "deploy-backend"], False),
        ("deploy.yml", [], True),
    ]


def test_ssh_command_is_dispatched(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(_site: Site, extra: list[str], _reader: object) -> int:
        return len(extra)

    monkeypatch.setattr(commands, "open_ssh", fake)
    result = cli.main(["--sites-dir", str(site.directory.parent), "ssh", "test", "-v"])
    assert result == 1


def test_vault_command_is_dispatched(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, list[str]]] = []

    def fake(_site: Site, action: str, files: list[str], _reader: object) -> int:
        seen.append((action, files))
        return 0

    monkeypatch.setattr(commands, "run_vault", fake)
    cli.main(
        ["--sites-dir", str(site.directory.parent), "vault", "test", "view", "a", "b"]
    )
    assert seen == [("view", ["a", "b"])]


def test_init_command_reports_the_created_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, str | None]] = []

    def fake(_sites: Path, stage: str, environment: str | None, _r: object) -> Path:
        seen.append((stage, environment))
        return tmp_path / stage

    monkeypatch.setattr(commands, "init_site", fake)
    result = cli.main(
        ["--sites-dir", str(tmp_path), "init", "qa", "--environment", "qa"],
        make_reader(),
    )
    assert result == 0
    assert seen == [("qa", "qa")]
    assert str(tmp_path / "qa") in capsys.readouterr().out


def test_init_without_environment_passes_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []

    def fake(_s: Path, _stage: str, environment: str | None, _r: object) -> Path:
        seen.append(environment)
        return tmp_path

    monkeypatch.setattr(commands, "init_site", fake)
    cli.main(["--sites-dir", str(tmp_path), "init", "qa"], make_reader())
    assert seen == [None]


def test_errors_are_printed_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = cli.main(["--sites-dir", str(tmp_path / "none"), "check", "test"])
    assert result == 1
    assert capsys.readouterr().err.startswith("osa: The site directory")


def test_unknown_vault_action_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit):
        cli.main(["vault", "test", "shred", "x"])


def test_default_sites_directory_is_next_to_the_repository() -> None:
    assert cli.DEFAULT_SITES_DIR.name == "sites"
    assert cli.DEFAULT_SITES_DIR.parent.name == "osa-access"
