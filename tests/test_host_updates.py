"""Security updates install themselves, and a restart they need follows at night."""

import re
from typing import cast

import yaml

from osa_tools.playbooks import PLAYBOOK_DIR

REBOOT_TASK = "Restart at night when an update requires it (Debian)"
# Nightly jobs the restart must keep clear of, as hour:minute in local time.
STATISTICS_CLOSE = (0, 5)
DEFAULT_BACKUP = (3, 0)


def _tasks() -> list[dict[str, object]]:
    plays = cast(
        "list[dict[str, object]]",
        yaml.safe_load((PLAYBOOK_DIR / "setup_vps.yml").read_text(encoding="utf-8")),
    )
    return cast("list[dict[str, object]]", plays[0]["tasks"])


def _reboot_settings() -> dict[str, str]:
    (task,) = (task for task in _tasks() if task.get("name") == REBOOT_TASK)
    assert task["when"] == 'ansible_os_family == "Debian"'
    module = cast("dict[str, str]", task["ansible.builtin.copy"])
    # Debian's own defaults live in 50unattended-upgrades; a later file wins.
    assert re.fullmatch(r"/etc/apt/apt\.conf\.d/([6-9]\d)\S+", module["dest"])
    return dict(
        re.findall(
            r'^Unattended-Upgrade::([\w-]+) "([^"]*)";$',
            module["content"],
            re.MULTILINE,
        )
    )


def test_the_host_restarts_by_itself_when_an_update_needs_it() -> None:
    settings = _reboot_settings()
    assert settings["Automatic-Reboot"] == "true"
    # A forgotten SSH session must not hold the kernel update back forever.
    assert settings["Automatic-Reboot-WithUsers"] == "true"


def test_the_restart_falls_between_the_statistics_and_the_backup() -> None:
    hour, minute = map(int, _reboot_settings()["Automatic-Reboot-Time"].split(":"))
    assert STATISTICS_CLOSE < (hour, minute) < DEFAULT_BACKUP
