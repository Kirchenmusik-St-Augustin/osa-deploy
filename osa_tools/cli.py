"""Argument parsing and dispatch for ./osa."""

import argparse
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path

from osa_tools import commands
from osa_tools.commands import PasswordReader
from osa_tools.errors import OsaError
from osa_tools.playbooks import REPOSITORY_ROOT
from osa_tools.site import load_site

DEFAULT_SITES_DIR = REPOSITORY_ROOT.parent / "osa-access" / "sites"
_VAULT_ACTIONS = ("encrypt", "decrypt", "edit", "view", "rekey")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="osa", description="Operate the OSA stages through their playbooks."
    )
    parser.add_argument(
        "--sites-dir",
        type=Path,
        default=DEFAULT_SITES_DIR,
        help="directory holding one folder per stage (default: ../osa-access/sites)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("setup", "prepare a bare VPS (setup_vps.yml)"),
        ("deploy", "sync secrets, units and Caddyfile (deploy.yml)"),
        ("check", "dry run of deploy.yml (--check --diff)"),
        ("ssh", "open a shell as the service user"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("stage")
        command.add_argument(
            "extra",
            nargs=argparse.REMAINDER,
            help="passed unchanged to ansible-playbook (ssh for 'ssh')",
        )

    vault_parser = sub.add_parser("vault", help="work on vault-encrypted files")
    vault_parser.add_argument("stage")
    vault_parser.add_argument("action", choices=_VAULT_ACTIONS)
    vault_parser.add_argument(
        "files", nargs="*", help="paths relative to the stage directory"
    )

    init = sub.add_parser("init", help="create a new stage from the template")
    init.add_argument("stage")
    init.add_argument(
        "--environment",
        help="production, qa or test (default: the stage name)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    read_password: PasswordReader = getpass.getpass,
) -> int:
    """Run one command and return the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args, read_password)
    except OsaError as exc:
        print(f"osa: {exc}", file=sys.stderr)  # noqa: T201
        return 1


def _dispatch(args: argparse.Namespace, read_password: PasswordReader) -> int:
    sites_dir = Path(str(args.sites_dir))
    stage = str(args.stage)
    command = str(args.command)
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
