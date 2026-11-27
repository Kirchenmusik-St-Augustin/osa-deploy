"""Read Caddy's JSON access log into plain request records."""

import gzip
import json
import logging
from collections.abc import Iterator
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

LOG = logging.getLogger(__name__)
CURRENT_LOG = "access.log"
ROLLED_LOGS = "access-*.log.gz"


@dataclass(frozen=True, slots=True)
class Request:
    """One handled request, with the time already in the statistics' zone."""

    time: datetime
    ip: str
    host: str
    method: str
    path: str
    query: str
    protocol: str
    status: int
    size: int
    duration: float
    user_agent: str
    referer: str


def _first_header(headers: dict[str, object], name: str) -> str:
    values = headers.get(name)
    if not isinstance(values, list) or not values:
        return ""
    first = cast("list[object]", values)[0]
    return first if isinstance(first, str) else ""


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def parse_line(line: str, zone: ZoneInfo) -> Request | None:
    """One log line, or None when it is not a complete request record."""
    try:
        record: object = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    entry = cast("dict[str, object]", record)
    request = entry.get("request")
    if not isinstance(request, dict):
        return None
    fields = cast("dict[str, object]", request)
    headers = fields.get("headers")
    header_map = cast("dict[str, object]", headers) if isinstance(headers, dict) else {}
    timestamp = _number(entry.get("ts"))
    status = _number(entry.get("status"))
    ip = fields.get("client_ip") or fields.get("remote_ip")
    host = fields.get("host")
    method = fields.get("method")
    uri = fields.get("uri")
    if (
        timestamp is None
        or status is None
        or not isinstance(ip, str)
        or not isinstance(host, str)
        or not isinstance(method, str)
        or not isinstance(uri, str)
    ):
        return None
    parts = urlsplit(uri)
    protocol = fields.get("proto")
    return Request(
        time=datetime.fromtimestamp(timestamp, zone),
        ip=ip,
        host=host.split(":", 1)[0].lower(),
        method=method,
        path=parts.path or "/",
        query=parts.query,
        protocol=protocol if isinstance(protocol, str) else "",
        status=int(status),
        size=int(_number(entry.get("size")) or 0),
        duration=_number(entry.get("duration")) or 0.0,
        user_agent=_first_header(header_map, "User-Agent"),
        referer=_first_header(header_map, "Referer"),
    )


def log_files(directory: Path) -> list[Path]:
    """The rolled, compressed parts first (oldest name first), then the live file."""
    files = sorted(directory.glob(ROLLED_LOGS))
    current = directory / CURRENT_LOG
    if current.is_file():
        files.append(current)
    return files


def _lines(path: Path) -> Iterator[str]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as stream:
            yield from stream
        return
    with path.open(encoding="utf-8", errors="replace") as stream:
        yield from stream


@dataclass
class LogReading:
    """The requests of every day not yet finished, by day, plus a count of bad lines."""

    days: dict[date, list[Request]]
    malformed: int


def read_unfinished(
    directory: Path, finished: AbstractSet[date], zone: ZoneInfo
) -> LogReading:
    """Reads every log file once; days already finished are skipped."""
    reading = LogReading(days={}, malformed=0)
    for path in log_files(directory):
        for line in _lines(path):
            if not line.strip():
                continue
            request = parse_line(line, zone)
            if request is None:
                reading.malformed += 1
                continue
            day = request.time.date()
            if day not in finished:
                reading.days.setdefault(day, []).append(request)
    for requests in reading.days.values():
        requests.sort(key=lambda request: request.time)
    if reading.malformed:
        LOG.warning("skipped %d unreadable log lines", reading.malformed)
    return reading
