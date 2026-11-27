"""The journal keeps the access logs, with full addresses, for at most 14 days.

The privacy statement of the static website names that limit, so the
settings are checked by their effect: the task of setup_vps.yml runs through
Ansible against a copy of Debian's journald.conf.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

from osa_tools.playbooks import PLAYBOOK_DIR

TASK_NAME = "Keep the journal for at most 14 days"
PROMISED_DAYS = 14
# Debian ships every setting commented out below its section header.
DEBIAN_DEFAULT = """[Journal]
#Storage=auto
#MaxRetentionSec=0
#MaxFileSec=1month
"""


def _play() -> dict[str, object]:
    plays = cast(
        "list[dict[str, object]]",
        yaml.safe_load((PLAYBOOK_DIR / "setup_vps.yml").read_text(encoding="utf-8")),
    )
    return plays[0]


def _task() -> dict[str, object]:
    tasks = cast("list[dict[str, object]]", _play()["tasks"])
    (task,) = (task for task in tasks if task.get("name") == TASK_NAME)
    return task


def _settings() -> dict[str, str]:
    items = cast("list[dict[str, str]]", _task()["loop"])
    return {item["key"]: item["value"] for item in items}


def _days(value: str) -> int:
    match = re.fullmatch(r"(\d+)day", value)
    assert match, value
    return int(match.group(1))


def test_the_oldest_entry_is_gone_within_the_promised_days() -> None:
    """Archived files go as a whole once they hold an entry past the retention;
    the current file stays until the next one starts, and journald checks then."""
    settings = _settings()
    retention = _days(settings["MaxRetentionSec"])
    per_file = _days(settings["MaxFileSec"])
    assert retention + per_file + per_file <= PROMISED_DAYS


def test_journald_restarts_after_a_change() -> None:
    assert _task()["notify"] == "restart journald"
    handlers = cast("list[dict[str, object]]", _play()["handlers"])
    assert "restart journald" in [handler["name"] for handler in handlers]


def _apply(tmp_path: Path, config: Path) -> subprocess.CompletedProcess[str]:
    task = {key: value for key, value in _task().items() if key != "notify"}
    module = cast("dict[str, object]", task["ansible.builtin.lineinfile"])
    task["ansible.builtin.lineinfile"] = {**module, "path": str(config)}
    playbook = tmp_path / "journal.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "tasks": [task],
                }
            ]
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(playbook)],
        env={
            **os.environ,
            "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp"),
            "ANSIBLE_NOCOLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("existing", [DEBIAN_DEFAULT, None], ids=["debian", "missing"])
def test_the_settings_land_in_the_journal_section_once(
    tmp_path: Path, existing: str | None
) -> None:
    config = tmp_path / "journald.conf"
    if existing is not None:
        config.write_text(existing, encoding="utf-8")
    first = _apply(tmp_path, config)
    assert first.returncode == 0, first.stdout
    lines = config.read_text(encoding="utf-8").splitlines()
    for key, value in _settings().items():
        assert [line for line in lines if line.lstrip("#").startswith(f"{key}=")] == [
            f"{key}={value}"
        ]
    second = _apply(tmp_path, config)
    assert second.returncode == 0, second.stdout
    assert "changed=0" in second.stdout
