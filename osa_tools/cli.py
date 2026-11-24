"""Argument parsing and dispatch for ./osa."""

import argparse
import getpass
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from osa_tools import commands
from osa_tools.commands import PasswordReader
from osa_tools.errors import OsaError
from osa_tools.playbooks import REPOSITORY_ROOT
from osa_tools.site import ENVIRONMENTS, load_site

DEFAULT_SITES_DIR = REPOSITORY_ROOT.parent / "osa-access" / "sites"
_VAULT_ACTIONS = ("encrypt", "decrypt", "edit", "view", "rekey")
_HELP_FLAGS = ("-h", "--help")

# The opt-in tags of deploy.yml (tagged "never", so they only run when named).
# A test keeps this mapping equal to the tags of the playbook.
DEPLOY_TAGS = {
    "deploy-backend": "pull the backend image now, restart its pod if it changed",
    "deploy-frontend": "pull the frontend image now, restart its pod if it changed",
}

_STAGE_HELP = "folder of the stage under the sites directory, e.g. production"
_ANSIBLE_METAVAR = "ANSIBLE_OPTION"
_ANSIBLE_HELP = (
    "further ansible-playbook options, passed through unchanged "
    "(all of them: ansible-playbook --help)"
)
_SSH_METAVAR = "SSH_OPTION"
_SSH_HELP = "ssh options, placed before the host; a remote command is not supported"

_MAIN_EPILOG = """\
a new stage, in this order:
  init -> vault encrypt (secrets) -> setup -> check -> deploy

day to day:
  check, then deploy. The application images follow :latest through the
  nightly podman-auto-update timer on the host; a plain deploy does not
  pull them (see "./osa deploy --help" for the immediate way).

Run "./osa <command> --help" for the options and examples of one command.
"""

# Descriptions and epilogs are printed as written (RawDescriptionHelpFormatter
# keeps the layout of the examples), so they carry their own line breaks.
_SETUP_DESCRIPTION = """\
Prepare a VPS with setup_vps.yml: packages, the admin and service users,
the operator keys, SSH hardening, firewall and Podman.
"""
_SETUP_EPILOG = """\
examples:
  ./osa setup production -u root --ask-pass -e full_upgrade=true
      first run on a fresh host, as root with the provider's password
  ./osa setup production -u admin --ask-become-pass --check --diff
      later runs: verify the host, asks for the sudo password of admin
"""

_DEPLOY_DESCRIPTION = """\
Apply deploy.yml to the stage: render the secrets, Quadlet units, Caddyfile
and timers, and restart what changed. A plain deploy pulls no application
images; the nightly podman-auto-update timer does that, or one of the tags
below does it right away.
"""
_CHECK_DESCRIPTION = """\
Dry run of deploy.yml (--check --diff): shows what deploy would change and
changes nothing on the host.
"""
_CHECK_EPILOG = """\
examples:
  ./osa check production
  ./osa check production -e osa_backend_image_tag=<full-commit-sha>
"""

_SSH_DESCRIPTION = """\
Open an interactive shell on the stage's host as the service user, with the
operator key of the stage.
"""
_SSH_EPILOG = """\
examples:
  ./osa ssh production
  ./osa ssh production -v      verbose ssh output for connection problems

On the host, "su - admin" switches to the user with sudo rights.
"""

_VAULT_DESCRIPTION = "Work on the vault-encrypted files of a stage."
_VAULT_EPILOG = """\
actions:
  encrypt   encrypt the named plain files in place
  decrypt   decrypt the named files in place (encrypt them again afterwards)
  edit      open the named files decrypted in $EDITOR, save encrypted
  view      print the named files decrypted
  rekey     re-encrypt every vault file of the stage under a new password
            (takes no files)

examples:
  ./osa vault production view secrets/osa-backend.env.j2
  ./osa vault production edit secrets/caddy.env
  ./osa vault production rekey

The access note credentials.md has its own command: ./osa credentials --help
"""

_CREDENTIALS_DESCRIPTION = """\
Work on the access note credentials.md in the root of the site repository
(next to the sites directory). It is encrypted with the same vault password
as the stages.
"""
_CREDENTIALS_EPILOG = """\
actions:
  view    print the note decrypted, long lines in full (no pager)
  edit    open the note decrypted in $EDITOR, save encrypted
  rekey   re-encrypt the note under a new vault password; the stages are
          rekeyed on their own with "./osa vault <stage> rekey"

examples:
  ./osa credentials view
  EDITOR=nano ./osa credentials edit
  ./osa credentials rekey

The longer form "./osa vault production edit ../../credentials.md" works too.
"""

_INIT_DESCRIPTION = """\
Create <sites directory>/<stage> from site-template with a fresh operator
key; asks for the new vault password of the stage.
"""
_INIT_EPILOG = """\
examples:
  ./osa init qa
  ./osa init production-new --environment production
"""


def _deploy_epilog() -> str:
    width = max(len(tag) for tag in DEPLOY_TAGS)
    tags = "\n".join(f"  {tag:<{width}}  {text}" for tag, text in DEPLOY_TAGS.items())
    return f"""\
tags (pass with --tags, several separated by commas):
{tags}
  add -e force_restart=true to restart the pod even when the image is unchanged

examples:
  ./osa deploy production
  ./osa deploy production --tags deploy-frontend
  ./osa deploy production --tags deploy-backend,deploy-frontend
  ./osa deploy production -e osa_backend_image_tag=<full-commit-sha>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="osa",
        description="Operate the OSA stages through their playbooks.",
        epilog=_MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sites-dir",
        type=Path,
        default=DEFAULT_SITES_DIR,
        metavar="DIR",
        help="directory holding one folder per stage (default: ../osa-access/sites)",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    parsers = {
        name: sub.add_parser(
            name,
            help=text.summary,
            description=text.description,
            epilog=text.epilog,
            usage=text.usage,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        for name, text in _command_help().items()
    }
    for name in ("setup", "check", "deploy"):
        _add_pass_through(parsers[name], _ANSIBLE_METAVAR, _ANSIBLE_HELP)
    _add_pass_through(parsers["ssh"], _SSH_METAVAR, _SSH_HELP)
    _add_vault_arguments(parsers["vault"])
    parsers["credentials"].add_argument(
        "action",
        choices=commands.CREDENTIALS_ACTIONS,
        metavar="ACTION",
        help=f"one of {', '.join(commands.CREDENTIALS_ACTIONS)}, see actions below",
    )
    _add_init_arguments(parsers["init"])
    return parser


@dataclass(frozen=True)
class _CommandHelp:
    summary: str
    description: str
    epilog: str
    usage: str | None = None


def _pass_through_usage(metavar: str) -> str:
    # argparse prints a REMAINDER argument as a bare "..." in the usage line,
    # whatever its metavar; the explicit usage names the pass-through part.
    return f"%(prog)s [-h] stage [{metavar} ...]"


def _command_help() -> dict[str, _CommandHelp]:
    """Help texts per command, in the order of the command list."""
    ansible_usage = _pass_through_usage(_ANSIBLE_METAVAR)
    return {
        "setup": _CommandHelp(
            "prepare a VPS (setup_vps.yml)",
            _SETUP_DESCRIPTION,
            _SETUP_EPILOG,
            ansible_usage,
        ),
        "check": _CommandHelp(
            "dry run of deploy.yml, changes nothing",
            _CHECK_DESCRIPTION,
            _CHECK_EPILOG,
            ansible_usage,
        ),
        "deploy": _CommandHelp(
            "sync secrets, units, Caddyfile and timers (deploy.yml)",
            _DEPLOY_DESCRIPTION,
            _deploy_epilog(),
            ansible_usage,
        ),
        "ssh": _CommandHelp(
            "open a shell on the host as the service user",
            _SSH_DESCRIPTION,
            _SSH_EPILOG,
            _pass_through_usage(_SSH_METAVAR),
        ),
        "vault": _CommandHelp(
            "work on vault-encrypted files", _VAULT_DESCRIPTION, _VAULT_EPILOG
        ),
        "credentials": _CommandHelp(
            "view, edit or rekey the access note credentials.md",
            _CREDENTIALS_DESCRIPTION,
            _CREDENTIALS_EPILOG,
        ),
        "init": _CommandHelp(
            "create a new stage from the template", _INIT_DESCRIPTION, _INIT_EPILOG
        ),
    }


def _add_pass_through(
    command: argparse.ArgumentParser, metavar: str, help_text: str
) -> None:
    command.add_argument("stage", help=_STAGE_HELP)
    command.add_argument(
        "extra", nargs=argparse.REMAINDER, metavar=metavar, help=help_text
    )


def _add_vault_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("stage", help=_STAGE_HELP)
    command.add_argument(
        "action",
        choices=_VAULT_ACTIONS,
        metavar="ACTION",
        help=f"one of {', '.join(_VAULT_ACTIONS)}, see actions below",
    )
    command.add_argument(
        "files",
        nargs="*",
        metavar="FILE",
        help="paths relative to the stage directory, e.g. secrets/caddy.env",
    )


def _add_init_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("stage", help="name of the new stage folder")
    command.add_argument(
        "--environment",
        metavar="ENV",
        help=f"one of {', '.join(ENVIRONMENTS)} (default: the stage name)",
    )


def main(
    argv: Sequence[str] | None = None,
    read_password: PasswordReader = getpass.getpass,
) -> int:
    """Run one command and return the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if _asks_for_help(args):
        # The pass-through arguments would otherwise swallow -h/--help and
        # hand it to ansible-playbook or ssh after the vault password prompt.
        parser.parse_args([str(args.command), "--help"])
    try:
        return _dispatch(args, read_password)
    except OsaError as exc:
        print(f"osa: {exc}", file=sys.stderr)  # noqa: T201
        return 1


def _asks_for_help(args: argparse.Namespace) -> bool:
    extra: list[str] = [str(item) for item in getattr(args, "extra", [])]
    return any(item in _HELP_FLAGS for item in extra)


def _dispatch(args: argparse.Namespace, read_password: PasswordReader) -> int:
    sites_dir = Path(str(args.sites_dir))
    command = str(args.command)
    if command == "credentials":
        return commands.run_credentials(sites_dir, str(args.action), read_password)
    stage = str(args.stage)
    if command == "init":
        environment = None if args.environment is None else str(args.environment)
        created = commands.init_site(sites_dir, stage, environment, read_password)
        print(f"Created {created}. Next: fill in inventory.ini and the secrets.")  # noqa: T201
        return 0
    site = load_site(sites_dir, stage)
    if command == "vault":
        files: list[str] = [str(item) for item in args.files]
        return commands.run_vault(site, str(args.action), files, read_password)
    extra: list[str] = [str(item) for item in args.extra]
    if command == "ssh":
        return commands.open_ssh(site, extra, read_password)
    playbook = "setup_vps.yml" if command == "setup" else "deploy.yml"
    return commands.run_playbook(
        site, playbook, extra, read_password, dry_run=command == "check"
    )
