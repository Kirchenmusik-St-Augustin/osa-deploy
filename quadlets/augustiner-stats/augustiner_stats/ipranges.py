"""Sorted IP address ranges with a two-letter value, looked up without loading.

The geodata job turns the DB-IP lists into one file per address family:
fixed-width records (start, end, value) in ascending order. A lookup is a
binary search over the memory-mapped file, so a run needs a few kilobytes
instead of the hundreds of megabytes the lists would take as Python objects.
"""

import ipaddress
import mmap
from collections.abc import Iterable
from pathlib import Path

VALUE_BYTES = 2
MAGIC = b"AUGRANGE1"


def _width(version: int) -> int:
    return 4 if version == 4 else 16


def write_table(path: Path, version: int, rows: Iterable[tuple[int, int, str]]) -> int:
    """Writes ascending, non-overlapping ranges; returns how many were written."""
    width = _width(version)
    previous_end = -1
    count = 0
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(MAGIC + bytes([version]))
        for start, end, value in rows:
            encoded = value.encode("ascii")
            if len(encoded) != VALUE_BYTES or start > end or start <= previous_end:
                msg = f"range {start}-{end} {value!r} is malformed or out of order"
                raise ValueError(msg)
            stream.write(start.to_bytes(width) + end.to_bytes(width) + encoded)
            previous_end = end
            count += 1
    temporary.replace(path)
    return count


class RangeTable:
    """Read-only lookup in one file written by write_table."""

    def __init__(self, path: Path, version: int) -> None:
        self._width = _width(version)
        self._record = 2 * self._width + VALUE_BYTES
        self._header = len(MAGIC) + 1
        with path.open("rb") as stream:
            self._map = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        if self._map[: self._header] != MAGIC + bytes([version]):
            self._map.close()
            msg = f"{path} is not a range table for IPv{version}"
            raise ValueError(msg)
        self._count = (len(self._map) - self._header) // self._record

    def __len__(self) -> int:
        return self._count

    def _bounds(self, index: int) -> tuple[int, int, bytes]:
        offset = self._header + index * self._record
        record = self._map[offset : offset + self._record]
        start = int.from_bytes(record[: self._width])
        end = int.from_bytes(record[self._width : 2 * self._width])
        return start, end, record[2 * self._width :]

    def lookup(self, address: int) -> str | None:
        low, high = 0, self._count - 1
        while low <= high:
            middle = (low + high) // 2
            start, end, value = self._bounds(middle)
            if address < start:
                high = middle - 1
            elif address > end:
                low = middle + 1
            else:
                return value.decode("ascii")
        return None

    def close(self) -> None:
        self._map.close()


class AddressLookup:
    """Both address families; missing files simply answer None."""

    def __init__(self, v4: Path, v6: Path) -> None:
        self._tables = {
            version: RangeTable(path, version)
            for version, path in ((4, v4), (6, v6))
            if path.is_file()
        }

    @property
    def available(self) -> bool:
        return len(self._tables) == 2

    def lookup(self, ip: str) -> str | None:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return None
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        table = self._tables.get(address.version)
        return table.lookup(int(address)) if table else None

    def close(self) -> None:
        for table in self._tables.values():
            table.close()
