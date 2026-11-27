"""Pseudonymous addresses and visitor ids from a secret that lives one month.

Within a calendar month the same address and browser give the same visitor
id, so the month's unique visitors can be counted. Once the month is closed
its secret is deleted: from then on nobody can tell which address a stored
value stood for.
"""

import hashlib
import hmac
import ipaddress
import os
import secrets
from datetime import date
from pathlib import Path

SECRET_BYTES = 32
# Stands in for an unreadable address; a value, nothing ever listens on it.
UNKNOWN_ADDRESS = "0.0.0.0"  # noqa: S104
_PREFIX = "secret-"


def month_of(day: date) -> str:
    return f"{day:%Y-%m}"


class MonthSecrets:
    """One random secret per calendar month, files readable by the owner only."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _path(self, month: str) -> Path:
        return self._directory / f"{_PREFIX}{month}"

    def for_day(self, day: date) -> bytes:
        path = self._path(month_of(day))
        if path.is_file():
            return path.read_bytes()
        value = secrets.token_bytes(SECRET_BYTES)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
        return value

    def months(self) -> list[str]:
        return sorted(
            path.name.removeprefix(_PREFIX)
            for path in self._directory.glob(f"{_PREFIX}*")
        )

    def forget(self, month: str) -> None:
        self._path(month).unlink(missing_ok=True)


def _digest(secret: bytes, *parts: str) -> bytes:
    return hmac.new(secret, "\x1f".join(parts).encode(), hashlib.sha256).digest()


def pseudonymous_address(ip: str, secret: bytes) -> str:
    """Keeps the network (/24 or /48) and replaces the rest with keyed hash bits.

    The network still tells the country and the provider; the replaced part
    keeps different visitors of one network apart for the month.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return UNKNOWN_ADDRESS
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    digest = int.from_bytes(_digest(secret, "address", address.compressed))
    if isinstance(address, ipaddress.IPv6Address):
        network = int(address) >> 80 << 80
        return str(ipaddress.IPv6Address(network | digest % (1 << 80)))
    network = int(address) >> 8 << 8
    return str(ipaddress.IPv4Address(network | digest % 256))


def visitor_id(ip: str, user_agent: str, secret: bytes) -> str:
    return _digest(secret, "visitor", ip, user_agent).hex()[:16]
