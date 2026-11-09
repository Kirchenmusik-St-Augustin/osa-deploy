"""Both playbooks must parse against a stage created by `osa init`."""

import json
import subprocess
from pathlib import Path

import pytest

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
