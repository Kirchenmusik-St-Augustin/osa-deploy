"""Both playbooks must parse against a stage created by `osa init`."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from osa_tools import commands
from osa_tools.playbooks import PLAYBOOK_DIR, REPOSITORY_ROOT
from tests.conftest import PASSWORD, make_reader


@pytest.fixture
def initialised_stage(tmp_path: Path) -> Path:
    sites = tmp_path / "sites"
    sites.mkdir()
    return commands.init_site(sites, "test", None, make_reader(PASSWORD, PASSWORD))


@pytest.mark.parametrize("playbook", ["deploy.yml", "setup_vps.yml"])
def test_playbook_passes_the_syntax_check(
    initialised_stage: Path, playbook: str
) -> None:
    result = subprocess.run(
        [
            "ansible-playbook",
            "--syntax-check",
            "-i",
            str(initialised_stage / "inventory.ini"),
            "-e",
            json.dumps({"osa_site_dir": str(initialised_stage)}),
            str(PLAYBOOK_DIR / playbook),
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _first_play(playbook: str) -> dict[str, Any]:
    plays = yaml.safe_load((PLAYBOOK_DIR / playbook).read_text(encoding="utf-8"))
    return plays[0]


def test_deploy_enables_the_nightly_auto_update_timer() -> None:
    """The timer ships with the podman package switched off; the application
    containers only follow `latest` if deploy.yml turns it on."""
    play = _first_play("deploy.yml")
    assert "podman-auto-update.timer" in play["vars"]["managed_units_safe"]


def test_setup_makes_network_online_reachable_on_ifupdown_hosts() -> None:
    """The system network-online.target only becomes active when a wait-online
    service is enabled and some unit requests the target. Without both, the
    rootless wait unit in front of every Quadlet container times out after
    90 s on each start, after every reboot as well."""
    play = _first_play("setup_vps.yml")
    systemd_tasks = {
        task["ansible.builtin.systemd"]["name"]: task
        for task in play["tasks"]
        if "ansible.builtin.systemd" in task
    }
    enable_task = systemd_tasks["ifupdown-wait-online.service"]
    assert enable_task["ansible.builtin.systemd"]["enabled"] is True
    assert "ifupdown_networking_unit.stat.exists" in enable_task["when"]
    drop_in_tasks = [
        task
        for task in play["tasks"]
        if task.get("ansible.builtin.copy", {})
        .get("dest", "")
        .startswith("/etc/systemd/system/multi-user.target.d/")
    ]
    assert len(drop_in_tasks) == 1
    drop_in = drop_in_tasks[0]["ansible.builtin.copy"]
    assert "Wants=network-online.target" in drop_in["content"]
    assert "ifupdown_networking_unit.stat.exists" in drop_in_tasks[0]["when"]
    start_task = systemd_tasks["network-online.target"]
    assert start_task["ansible.builtin.systemd"]["state"] == "started"
    assert "ifupdown_networking_unit.stat.exists" in start_task["when"]
