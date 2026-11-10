"""The setup playbook must leave exactly the operator keys on the host."""

from typing import cast

import yaml

from osa_tools.playbooks import PLAYBOOK_DIR

_KEY_MODULE = "ansible.posix.authorized_key"
_SSHD_FILE = "/etc/ssh/sshd_config"


def _tasks() -> list[dict[str, object]]:
    plays = cast(
        "list[dict[str, object]]",
        yaml.safe_load((PLAYBOOK_DIR / "setup_vps.yml").read_text(encoding="utf-8")),
    )
    return cast("list[dict[str, object]]", plays[0]["tasks"])


def _key_tasks() -> list[dict[str, object]]:
    return [task for task in _tasks() if _KEY_MODULE in task]


def test_there_is_exactly_one_task_that_writes_authorized_keys() -> None:
    assert len(_key_tasks()) == 1


def test_the_key_task_removes_every_key_that_is_not_listed() -> None:
    arguments = cast("dict[str, object]", _key_tasks()[0][_KEY_MODULE])
    assert arguments["exclusive"] is True
    assert arguments["state"] == "present"


def test_the_key_task_covers_both_login_users() -> None:
    assert _key_tasks()[0]["loop"] == ["admin", "service"]


def test_the_keys_are_installed_before_password_login_is_switched_off() -> None:
    names = [str(task.get("name", "")) for task in _tasks()]
    key_position = names.index(
        "Install exactly the operator public keys for admin and service"
    )
    hardening_position = names.index(
        "Harden sshd (key-only authentication, no root login)"
    )
    assert key_position < hardening_position


def test_the_effective_sshd_check_is_skipped_only_when_a_dry_run_would_change_it() -> (
    None
):
    tasks = {str(task.get("name", "")): task for task in _tasks()}
    harden = tasks["Harden sshd (key-only authentication, no root login)"]
    assert_task = tasks["Assert that no drop-in file weakens the sshd hardening"]
    assert harden["register"] == "sshd_hardening"
    assert assert_task["when"] == "not ansible_check_mode or not sshd_hardening.changed"


def test_sshd_settings_are_inserted_before_the_managed_admin_block() -> None:
    tasks = {str(task.get("name", "")): task for task in _tasks()}
    harden = tasks["Harden sshd (key-only authentication, no root login)"]
    arguments = cast("dict[str, object]", harden["ansible.builtin.lineinfile"])
    assert arguments["firstmatch"] is True
    assert arguments["insertbefore"] == (
        "^(# BEGIN ANSIBLE MANAGED BLOCK - ADMIN SSH|Match )"
    )
