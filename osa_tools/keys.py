"""Operator SSH key generation."""

from pathlib import Path

from osa_tools.runner import capture


def generate_key(private_path: Path, comment: str) -> str:
    """Create an ed25519 key pair without a passphrase and return the public key.

    The private key is protected by the vault instead, see operator_key.vault.
    """
    capture(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            comment,
            "-f",
            str(private_path),
        ]
    )
    return private_path.with_name(private_path.name + ".pub").read_text(
        encoding="utf-8"
    )
