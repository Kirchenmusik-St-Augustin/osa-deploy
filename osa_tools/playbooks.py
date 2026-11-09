"""Command lines for ansible-playbook."""

import json
from collections.abc import Sequence
from pathlib import Path

from osa_tools.site import Site
from osa_tools.workspace import Session

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK_DIR = REPOSITORY_ROOT / "playbooks"


def build_argv(
    playbook: str,
    site: Site,
    session: Session,
    extra: Sequence[str],
    *,
    dry_run: bool,
) -> list[str]:
    """Assemble the ansible-playbook call for one stage.

    The site directory reaches the playbooks as the extra variable
    osa_site_dir; everything the operator typed after the stage is appended
    unchanged.
    """
    argv = [
        "ansible-playbook",
        "-i",
        str(site.inventory),
        "-e",
        json.dumps({"osa_site_dir": str(site.directory)}),
        "--vault-password-file",
        str(session.password_file),
        "--private-key",
        str(session.key_file),
        str(PLAYBOOK_DIR / playbook),
    ]
    if dry_run:
        argv += ["--check", "--diff"]
    return [*argv, *extra]
