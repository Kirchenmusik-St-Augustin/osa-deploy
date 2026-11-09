from pathlib import Path

import pytest

from osa_tools.errors import OsaError
from osa_tools.site import Site, load_site, read_host, validate_stage_name


@pytest.mark.parametrize("stage", ["production", "qa", "test", "production-new", "a"])
def test_valid_stage_names_are_accepted(stage: str) -> None:
    assert validate_stage_name(stage) == stage


@pytest.mark.parametrize(
    "stage", ["", "Prod", "../etc", "a/b", "1abc", "-x", "a" * 32, "with space"]
)
def test_invalid_stage_names_are_refused(stage: str) -> None:
    with pytest.raises(OsaError, match="not a valid stage name"):
        validate_stage_name(stage)


def test_longest_valid_stage_name_is_accepted() -> None:
    assert validate_stage_name("a" * 31) == "a" * 31


def test_site_paths_follow_the_stage_directory(tmp_path: Path) -> None:
    site = Site(name="qa", directory=tmp_path / "qa")
    assert site.inventory == tmp_path / "qa" / "inventory.ini"
    assert site.operator_key_vault == tmp_path / "qa" / "operator_key.vault"
    assert site.operator_public_keys == tmp_path / "qa" / "operator_keys.pub"
    assert site.secrets == tmp_path / "qa" / "secrets"


def test_load_site_returns_the_stage(site: Site) -> None:
    assert load_site(site.directory.parent, "test") == site


def test_load_site_without_sites_directory(tmp_path: Path) -> None:
    with pytest.raises(OsaError, match="does not exist"):
        load_site(tmp_path / "missing", "test")


@pytest.mark.parametrize("missing", ["inventory.ini", "operator_key.vault"])
def test_load_site_names_the_missing_file(site: Site, missing: str) -> None:
    (site.directory / missing).unlink()
    with pytest.raises(OsaError, match=f"{missing} is missing"):
        load_site(site.directory.parent, "test")


def test_load_site_rejects_a_bad_stage_name(tmp_path: Path) -> None:
    with pytest.raises(OsaError, match="not a valid stage name"):
        load_site(tmp_path, "../x")


def test_read_host_returns_the_first_host(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.ini"
    inventory.write_text(
        "# comment\n[other]\nignored.example\n[vps]\n; note\n\nhost.example "
        "ansible_user=x\nsecond.example\n",
        encoding="utf-8",
    )
    assert read_host(inventory) == "host.example"


def test_read_host_prefers_ansible_host(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.ini"
    inventory.write_text("[vps]\nalias ansible_host=192.0.2.7\n", encoding="utf-8")
    assert read_host(inventory) == "192.0.2.7"


def test_read_host_ignores_other_options(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.ini"
    inventory.write_text("[vps]\nname ansible_port=2222\n", encoding="utf-8")
    assert read_host(inventory) == "name"


def test_read_host_without_a_host(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.ini"
    inventory.write_text("[vps]\n[vps:vars]\nx=1\n", encoding="utf-8")
    with pytest.raises(OsaError, match="No host found"):
        read_host(inventory)
