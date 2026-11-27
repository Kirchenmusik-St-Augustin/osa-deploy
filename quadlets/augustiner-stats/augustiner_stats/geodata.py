"""Monthly download of the free DB-IP lists (CC BY 4.0, https://db-ip.com).

Four files per month: the country and the ASN list, each as CSV (turned into
the address tables of ipranges) and as MMDB (read by GoAccess). Each file is
checked before anything is replaced; when this month's files are not out
yet, last month's are taken; when nothing can be fetched the old files stay.
"""

import csv
import gzip
import io
import ipaddress
import logging
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from augustiner_stats.classify import is_hosting_network
from augustiner_stats.ipranges import write_table

LOG = logging.getLogger(__name__)
BASE_URL = "https://download.db-ip.com/free/"
LIST_NAMES = ("country", "asn")
SMALLEST_FILE = 1024 * 1024
LARGEST_FILE = 64 * 1024 * 1024
FEWEST_ROWS = 100_000
DATACENTER = "DC"
MMDB_MARKER = b"\xab\xcd\xefMaxMind.com"
VERSION_FILE = "VERSION"
TIMEOUT_SECONDS = 120

Fetcher = Callable[[str], bytes]


class GeodataError(RuntimeError):
    """No usable list could be fetched."""


def fetch(url: str) -> bytes:
    """Downloads one file over HTTPS, refusing anything larger than LARGEST_FILE."""
    if not url.startswith("https://"):
        msg = f"refusing a non-HTTPS address: {url}"
        raise GeodataError(msg)
    request = Request(url, headers={"User-Agent": "augustiner-stats geodata"})  # noqa: S310 - https only, checked above
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - https only, checked above
        body: bytes = response.read(LARGEST_FILE + 1)
    if len(body) > LARGEST_FILE:
        msg = f"{url} is larger than {LARGEST_FILE} bytes"
        raise GeodataError(msg)
    return body


def _unpacked(blob: bytes, url: str) -> bytes:
    try:
        data = gzip.decompress(blob)
    except (OSError, EOFError) as error:
        msg = f"{url} is not a gzip file"
        raise GeodataError(msg) from error
    if not SMALLEST_FILE <= len(data) <= LARGEST_FILE * 4:
        msg = f"{url} has an implausible size of {len(data)} bytes"
        raise GeodataError(msg)
    return data


@dataclass(frozen=True)
class Row:
    version: int
    start: int
    end: int
    value: str


def csv_records(data: bytes) -> Iterator[tuple[int, int, int, list[str]]]:
    """(version, start, end, other fields) one by one, decoded while reading."""
    text = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", newline="")
    for fields in csv.reader(text):
        start = ipaddress.ip_address(fields[0])
        end = ipaddress.ip_address(fields[1])
        if start.version != end.version:
            msg = f"range {fields[0]}-{fields[1]} mixes address families"
            raise GeodataError(msg)
        yield start.version, int(start), int(end), fields[2:]


def country_rows(data: bytes) -> Iterator[Row]:
    return (
        Row(version, start, end, rest[0])
        for version, start, end, rest in csv_records(data)
    )


def datacenter_rows(data: bytes) -> Iterator[Row]:
    """Only the ranges of hosting networks, marked with one shared value."""
    return (
        Row(version, start, end, DATACENTER)
        for version, start, end, (asn, *operator) in csv_records(data)
        if asn.isdigit() and is_hosting_network(int(asn), "".join(operator))
    )


def _write_family_tables(
    directory: Path, prefix: str, rows: Callable[[], Iterator[Row]]
) -> int:
    """One table per address family; the list is read once for each."""
    total = 0
    for version in (4, 6):
        total += write_table(
            directory / f"{prefix}-v{version}.bin",
            version,
            ((r.start, r.end, r.value) for r in rows() if r.version == version),
        )
    return total


def _month_names(today: date) -> tuple[str, str]:
    previous = today.replace(day=1) - timedelta(days=1)
    return f"{today:%Y-%m}", f"{previous:%Y-%m}"


def _download(name: str, kind: str, month: str, fetcher: Fetcher) -> bytes:
    url = f"{BASE_URL}dbip-{name}-lite-{month}.{kind}.gz"
    return _unpacked(fetcher(url), url)


def _stage_database(staging: Path, name: str, month: str, fetcher: Fetcher) -> None:
    content = _download(name, "mmdb", month, fetcher)
    if MMDB_MARKER not in content[-128 * 1024 :]:
        msg = f"the {name} database of {month} is not an MMDB file"
        raise GeodataError(msg)
    (staging / f"{name}.mmdb").write_bytes(content)


def _stage_tables(staging: Path, month: str, fetcher: Fetcher) -> None:
    """The address tables; the lists are released when this returns."""
    countries = _download("country", "csv", month, fetcher)
    datacenters = _download("asn", "csv", month, fetcher)
    try:
        count = _write_family_tables(
            staging, "country", lambda: country_rows(countries)
        )
        _write_family_tables(
            staging, "datacenter", lambda: datacenter_rows(datacenters)
        )
    except (ValueError, IndexError, UnicodeDecodeError) as error:
        msg = f"the lists of {month} cannot be read: {error}"
        raise GeodataError(msg) from error
    if count < FEWEST_ROWS:
        msg = f"the country list of {month} has only {count} rows"
        raise GeodataError(msg)


def _stage(staging: Path, month: str, fetcher: Fetcher) -> None:
    """Fetches, checks and converts every file of the month into staging/."""
    _stage_tables(staging, month, fetcher)
    for name in LIST_NAMES:
        _stage_database(staging, name, month, fetcher)
    (staging / VERSION_FILE).write_text(f"{month}\n", encoding="utf-8")


def _install(staging: Path, directory: Path) -> None:
    """Moves the staged files into place, the version file last."""
    files = sorted(staging.iterdir(), key=lambda path: path.name == VERSION_FILE)
    for path in files:
        path.replace(directory / path.name)
    staging.rmdir()


def update(directory: Path, today: date, fetcher: Fetcher = fetch) -> str:
    """Installs this month's lists, or last month's; returns the month used."""
    directory.mkdir(parents=True, exist_ok=True)
    for month in _month_names(today):
        staging = directory / f".staging-{month}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        try:
            _stage(staging, month, fetcher)
        except (GeodataError, URLError, OSError) as error:
            shutil.rmtree(staging, ignore_errors=True)
            LOG.warning("the lists of %s are not usable: %s", month, error)
            continue
        _install(staging, directory)
        LOG.info("installed the DB-IP lists of %s", month)
        return month
    msg = "neither this nor last month's DB-IP lists could be fetched"
    raise GeodataError(msg)
