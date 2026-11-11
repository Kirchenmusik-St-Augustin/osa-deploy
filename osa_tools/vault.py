"""ansible-vault calls; the password always travels as a file, never as an argument."""

from collections.abc import Sequence
from pathlib import Path

from osa_tools.errors import OsaError
from osa_tools.runner import capture, run

MIN_PASSWORD_LENGTH = 20
_VAULT_HEADER = "$ANSIBLE_VAULT;"
_MANUAL_ACTIONS = ("encrypt", "decrypt", "edit", "view")


def check_new_password(password: str) -> str:
    """Refuse passwords that are too short for a shared vault."""
    if len(password) < MIN_PASSWORD_LENGTH:
        msg = f"The vault password needs at least {MIN_PASSWORD_LENGTH} characters."
        raise OsaError(msg)
    return password


def decrypt_to(source: Path, password_file: Path, target: Path) -> None:
    """Decrypt one vault file into `target`; a wrong password is reported as such."""
    try:
        capture(
            [
                "ansible-vault",
                "decrypt",
                "--vault-password-file",
                str(password_file),
                "--output",
                str(target),
                str(source),
            ]
        )
    except OsaError as exc:
        msg = f"Could not decrypt {source.name}: wrong vault password?"
        raise OsaError(msg) from exc


def encrypt_to(source: Path, password_file: Path, target: Path) -> None:
    """Encrypt `source` into `target`, leaving `source` untouched."""
    capture(
        [
            "ansible-vault",
            "encrypt",
            "--vault-password-file",
            str(password_file),
            "--output",
            str(target),
            str(source),
        ]
    )


def run_manual(
    action: str, files: Sequence[str], password_file: Path, cwd: Path
) -> int:
    """Run encrypt, decrypt, edit or view on files relative to `cwd`."""
    if action not in _MANUAL_ACTIONS:
        msg = f"Unknown vault action '{action}'."
        raise OsaError(msg)
    if not files:
        msg = f"'vault {action}' needs at least one file."
        raise OsaError(msg)
    return run(
        ["ansible-vault", action, "--vault-password-file", str(password_file), *files],
        cwd=cwd,
    )


def vaulted_files(directory: Path) -> list[Path]:
    """Return every vault-encrypted file below `directory`, sorted."""
    return sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and _starts_with_vault_header(path)
    )


def rekey(files: Sequence[Path], old_file: Path, new_file: Path) -> int:
    """Re-encrypt the files under the new password."""
    if not files:
        msg = "No vault-encrypted files found to rekey."
        raise OsaError(msg)
    return run(
        [
            "ansible-vault",
            "rekey",
            "--vault-password-file",
            str(old_file),
            "--new-vault-password-file",
            str(new_file),
            *(str(path) for path in files),
        ]
    )


def _starts_with_vault_header(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(len(_VAULT_HEADER)) == _VAULT_HEADER.encode()
