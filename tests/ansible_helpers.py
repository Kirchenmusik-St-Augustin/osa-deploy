"""Run the Caddyfile template and single deploy.yml tasks through Ansible.

The tests see exactly what a deploy renders and asserts, without a host.
"""

import json
import os
import subprocess
from collections.abc import Collection, Mapping
from pathlib import Path

import yaml

from osa_tools.playbooks import PLAYBOOK_DIR

OSA_DOMAINS = {
    "backend_domain": "app.osa-tests.org",
    "frontend_domain": "app.osa-tests.org",
    "shorturl_domain": "go.osa-tests.org",
    "logging_domain": "logs.osa-tests.org",
    "acme_email": "ops@osa-tests.org",
}


def _ansible_env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp"),
        "ANSIBLE_NOCOLOR": "1",
    }


def render_caddyfile(tmp_path: Path, template: Path, **site: object) -> str:
    destination = tmp_path / "Caddyfile"
    variables = tmp_path / "vars.json"
    variables.write_text(json.dumps({**OSA_DOMAINS, **site}), encoding="utf-8")
    result = subprocess.run(
        [
            "ansible",
            "localhost",
            "-i",
            "localhost,",
            "-c",
            "local",
            "-m",
            "ansible.builtin.template",
            "-a",
            f"src={template} dest={destination}",
            "-e",
            f"@{variables}",
        ],
        env=_ansible_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return destination.read_text(encoding="utf-8")


def run_deploy_tasks(
    tmp_path: Path,
    wanted: set[str],
    omit: Collection[str] = (),
    /,
    **variables: object,
) -> subprocess.CompletedProcess[str]:
    """Runs the named tasks of deploy.yml on their own against localhost.

    The variables of OSA_DOMAINS named in omit stay undefined.
    """
    plays = yaml.safe_load((PLAYBOOK_DIR / "deploy.yml").read_text(encoding="utf-8"))
    tasks: list[Mapping[str, object]] = plays[0]["tasks"]
    selected = [task for task in tasks if task["name"] in wanted]
    assert len(selected) == len(wanted)
    playbook = tmp_path / "tasks.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "tasks": selected,
                }
            ]
        ),
        encoding="utf-8",
    )
    variables_file = tmp_path / "vars.json"
    variables_file.write_text(
        json.dumps(
            {
                **{key: value for key, value in OSA_DOMAINS.items() if key not in omit},
                **variables,
            }
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [
            "ansible-playbook",
            "-i",
            "localhost,",
            "-e",
            f"@{variables_file}",
            str(playbook),
        ],
        env=_ansible_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
