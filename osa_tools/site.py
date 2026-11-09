"""Model of one stage directory in the site repository."""

import re
from dataclasses import dataclass
from pathlib import Path

from osa_tools.errors import OsaError

ENVIRONMENTS = ("production", "qa", "test")
_STAGE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_HOST_SECTION = "[vps]"


@dataclass(frozen=True)
class Site:
    """The files of one stage: inventory, operator key and secrets."""

    name: str
    directory: Path

    @property
    def inventory(self) -> Path:
        return self.directory / "inventory.ini"

    @property
    def operator_key_vault(self) -> Path:
        return self.directory / "operator_key.vault"

    @property
    def operator_public_keys(self) -> Path:
        return self.directory / "operator_keys.pub"

    @property
    def secrets(self) -> Path:
        return self.directory / "secrets"


def validate_stage_name(stage: str) -> str:
    """Accept lower-case names only, so a stage can never escape sites_dir."""
    if not _STAGE_PATTERN.fullmatch(stage):
        msg = (
            f"'{stage}' is not a valid stage name (lower-case letters, digits "
            "and '-', starting with a letter, at most 31 characters)."
        )
        raise OsaError(msg)
    return stage


def load_site(sites_dir: Path, stage: str) -> Site:
    """Return the stage directory after checking that it is usable."""
    validate_stage_name(stage)
    if not sites_dir.is_dir():
        msg = (
            f"The site directory {sites_dir} does not exist. Clone the site "
            "repository next to this one or pass --sites-dir."
        )
        raise OsaError(msg)
    site = Site(name=stage, directory=sites_dir / stage)
    for required in (site.inventory, site.operator_key_vault):
        if not required.is_file():
            msg = f"{required} is missing. Create the stage with './osa init {stage}'."
            raise OsaError(msg)
    return site


def read_host(inventory: Path) -> str:
    """Return the first host of the [vps] group (honouring ansible_host)."""
    in_group = False
    for raw in inventory.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            in_group = line == _HOST_SECTION
            continue
        if not in_group:
            continue
        name, *options = line.split()
        for option in options:
            key, _, value = option.partition("=")
            if key == "ansible_host" and value:
                return value
        return name
    msg = f"No host found in the [vps] group of {inventory}."
    raise OsaError(msg)
