"""Caddy access log lines and requests for the statistics tests."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from augustiner_stats.accesslog import Request

VIENNA = ZoneInfo("Europe/Vienna")
FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1"
)
WINDOWS_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


def at(text: str) -> datetime:
    """A local Vienna time from "2026-11-26 10:00:00"."""
    return datetime.fromisoformat(text).replace(tzinfo=VIENNA)


def caddy_line(
    when: datetime,
    *,
    ip: str = "203.0.113.17",
    host: str = "augustinerkirche.at",
    uri: str = "/",
    method: str = "GET",
    status: int = 200,
    agent: str = FIREFOX,
    referer: str = "",
    proto: str = "HTTP/2.0",
) -> str:
    headers: dict[str, list[str]] = {"User-Agent": [agent]} if agent else {}
    if referer:
        headers["Referer"] = [referer]
    record = {
        "level": "info",
        "ts": when.timestamp(),
        "logger": "http.log.access.log0",
        "msg": "handled request",
        "request": {
            "remote_ip": ip,
            "client_ip": ip,
            "proto": proto,
            "method": method,
            "host": host,
            "uri": uri,
            "headers": headers,
        },
        "duration": 0.0015,
        "size": 5120,
        "status": status,
    }
    return json.dumps(record)


def request(
    when: str,
    path: str = "/",
    *,
    ip: str = "203.0.113.17",
    host: str = "augustinerkirche.at",
    method: str = "GET",
    status: int = 200,
    agent: str = FIREFOX,
    referer: str = "",
    query: str = "",
    protocol: str = "HTTP/2.0",
) -> Request:
    return Request(
        time=at(when),
        ip=ip,
        host=host,
        method=method,
        path=path,
        query=query,
        protocol=protocol,
        status=status,
        size=5120,
        duration=0.0015,
        user_agent=agent,
        referer=referer,
    )


def browsing(
    start: str,
    *paths: str,
    ip: str = "203.0.113.17",
    agent: str = FIREFOX,
    referer: str = "",
    step: timedelta = timedelta(minutes=1),
) -> list[Request]:
    """A person reading pages one step apart, each page fetching navigation.json."""
    first = at(start)
    visit: list[Request] = []
    for index, path in enumerate(paths):
        stamp = (first + index * step).strftime("%Y-%m-%d %H:%M:%S")
        visit.append(
            request(stamp, path, ip=ip, agent=agent, referer="" if index else referer)
        )
        visit.append(request(stamp, "/data/navigation.json", ip=ip, agent=agent))
    return visit
