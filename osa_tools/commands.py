"""The commands of the wrapper: playbook runs, ssh, vault access and init."""

import shutil
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path

from osa_tools import vault
from osa_tools.errors import OsaError
from osa_tools.keys import generate_key
from osa_tools.playbooks import REPOSITORY_ROOT, build_argv
from osa_tools.runner import run
from osa_tools.site import (
    ENVIRONMENTS,
    Site,
    read_host,
    validate_stage_name,
)
from osa_tools.workspace import Session, Workspace

PasswordReader = Callable[[str], str]

SITE_TEMPLATE_DIR = REPOSITORY_ROOT / "site-template"
_STAGE_PLACEHOLDER = "__STAGE__"
_PRIVATE_KEY_MODE = 0o600


@contextmanager
def open_session(site: Site, read_password: PasswordReader) -> Generator[Session]:
    """Ask for the vault password and unpack the operator key of the stage."""
    with Workspace() as workspace:
        password_file = workspace.write(
            "vault_pass", read_password("Vault password: ") + "\n"
        )
        key_file = workspace.path("operator_key")
        vault.decrypt_to(site.operator_key_vault, password_file, key_file)
        key_file.chmod(_PRIVATE_KEY_MODE)
        yield Session(password_file=password_file, key_file=key_file)


def run_playbook(
    site: Site,
    playbook: str,
    extra: Sequence[str],
    read_password: PasswordReader,
    *,
    dry_run: bool,
) -> int:
    """Run setup_vps.yml or deploy.yml against the stage."""
    with open_session(site, read_password) as session:
        argv = build_argv(playbook, site, session, extra, dry_run=dry_run)
        return run(argv, cwd=REPOSITORY_ROOT)


def open_ssh(site: Site, extra: Sequence[str], read_password: PasswordReader) -> int:
    """Open an interactive shell as the service user with the operator key."""
    host = read_host(site.inventory)
    with open_session(site, read_password) as session:
        return run(
            [
                "ssh",
                "-i",
                str(session.key_file),
                "-o",
                "IdentitiesOnly=yes",
                *extra,
                f"service@{host}",
            ]
        )


def run_vault(
    site: Site, action: str, files: Sequence[str], read_password: PasswordReader
) -> int:
    """Encrypt, decrypt, edit or view files of the stage, or rekey all of them."""
    if action == "rekey":
        return _rekey_stage(site, read_password)
    with Workspace() as workspace:
        password_file = workspace.write(
            "vault_pass", read_password("Vault password: ") + "\n"
        )
        return vault.run_manual(action, files, password_file, site.directory)


def _rekey_stage(site: Site, read_password: PasswordReader) -> int:
    with Workspace() as workspace:
        old_file = workspace.write(
            "old_pass", read_password("Current vault password: ") + "\n"
        )
        new_password = vault.check_new_password(read_password("New vault password: "))
        if read_password("Repeat the new vault password: ") != new_password:
            msg = "The two new passwords differ."
            raise OsaError(msg)
        new_file = workspace.write("new_pass", new_password + "\n")
        return vault.rekey(vault.vaulted_files(site.directory), old_file, new_file)


def init_site(
    sites_dir: Path,
    stage: str,
    environment: str | None,
    read_password: PasswordReader,
) -> Path:
    """Create sites/<stage> from the template with a fresh operator key."""
    validate_stage_name(stage)
    app_environment = _resolve_environment(stage, environment)
    if not sites_dir.is_dir():
        msg = f"The site directory {sites_dir} does not exist."
        raise OsaError(msg)
    target = sites_dir / stage
    if target.exists():
        msg = f"{target} already exists."
        raise OsaError(msg)
    password = vault.check_new_password(read_password("New vault password: "))
    if read_password("Repeat the vault password: ") != password:
        msg = "The two passwords differ."
        raise OsaError(msg)
    with Workspace() as workspace:
        password_file = workspace.write("vault_pass", password + "\n")
        public_key = generate_key(
            workspace.path("operator_key"), f"osa-operator-{stage}"
        )
        shutil.copytree(SITE_TEMPLATE_DIR, target)
        _fill_inventory(target / "inventory.ini", app_environment)
        vault.encrypt_to(
            workspace.path("operator_key"), password_file, target / "operator_key.vault"
        )
        (target / "operator_keys.pub").write_text(public_key, encoding="utf-8")
    return target


def _resolve_environment(stage: str, environment: str | None) -> str:
    chosen = environment if environment is not None else stage
    if chosen not in ENVIRONMENTS:
        msg = (
            f"'{chosen}' is not a deploy environment ({', '.join(ENVIRONMENTS)}). "
            "Pass --environment for a stage with a different directory name."
        )
        raise OsaError(msg)
    return chosen


def _fill_inventory(inventory: Path, app_environment: str) -> None:
    text = inventory.read_text(encoding="utf-8")
    inventory.write_text(
        text.replace(_STAGE_PLACEHOLDER, app_environment), encoding="utf-8"
    )
