"""Browser, operating system and device class from a User-Agent header.

Only the families matter here; full version strings would add little and
would make visitors easier to tell apart, so the reports keep the major
version at most.
"""

import re
from dataclasses import dataclass

OTHER = "Andere"

# Checked in this order: several browsers name others in their header
# (Edge and Opera say "Chrome", Chrome says "Safari").
_BROWSERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Samsung Internet", re.compile(r"SamsungBrowser/(\d+)")),
    ("Opera", re.compile(r"(?:OPR|Opera)/(\d+)")),
    ("Edge", re.compile(r"Edg(?:e|A|iOS)?/(\d+)")),
    ("Firefox", re.compile(r"(?:Firefox|FxiOS)/(\d+)")),
    ("Chrome", re.compile(r"(?:Chrome|CriOS)/(\d+)")),
    ("Safari", re.compile(r"Version/(\d+)[\d.]* (?:Mobile/\S+ )?Safari/")),
)
_SYSTEMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("iPadOS", re.compile(r"\biPad\b")),
    ("iOS", re.compile(r"\biPhone\b|\biPod\b")),
    ("Android", re.compile(r"\bAndroid\b")),
    ("Windows", re.compile(r"\bWindows\b")),
    ("ChromeOS", re.compile(r"\bCrOS\b")),
    ("macOS", re.compile(r"\bMacintosh\b|\bMac OS X\b")),
    ("Linux", re.compile(r"\bLinux\b")),
)
_TABLET = re.compile(r"\biPad\b|\bTablet\b")
_MOBILE = re.compile(r"\bMobile\b|\biPhone\b|\biPod\b")

PHONE = "Handy"
TABLET = "Tablet"
COMPUTER = "Computer"


@dataclass(frozen=True, slots=True)
class Agent:
    """What the reports keep of a User-Agent header."""

    browser: str
    major: int | None
    system: str
    device: str


def _browser(header: str) -> tuple[str, int | None]:
    for name, pattern in _BROWSERS:
        match = pattern.search(header)
        if match:
            return name, int(match.group(1))
    return OTHER, None


def _system(header: str) -> str:
    return next((name for name, pattern in _SYSTEMS if pattern.search(header)), OTHER)


def _device(header: str, system: str) -> str:
    if _TABLET.search(header) or (system == "Android" and not _MOBILE.search(header)):
        return TABLET
    if _MOBILE.search(header):
        return PHONE
    return COMPUTER


def parse_agent(header: str) -> Agent:
    browser, major = _browser(header)
    system = _system(header)
    return Agent(
        browser=browser, major=major, system=system, device=_device(header, system)
    )


# A header for each system that GoAccess recognises, without device details.
_SYSTEM_TOKENS = {
    "iPadOS": "iPad; CPU OS 18_0 like Mac OS X",
    "iOS": "iPhone; CPU iPhone OS 18_0 like Mac OS X",
    "Android": "Linux; Android 14; K",
    "Windows": "Windows NT 10.0; Win64; x64",
    "ChromeOS": "X11; CrOS x86_64",
    "macOS": "Macintosh; Intel Mac OS X 10_15_7",
    "Linux": "X11; Linux x86_64",
}
_WEBKIT = "AppleWebKit/537.36 (KHTML, like Gecko)"


def _browser_tokens(agent: Agent) -> str:
    version = agent.major or 0
    chrome = f"{_WEBKIT} Chrome/{version}.0.0.0 Safari/537.36"
    mobile = " Mobile" if agent.device == PHONE else ""
    tokens = {
        "Chrome": chrome,
        "Edge": f"{chrome} Edg/{version}.0.0.0",
        "Opera": f"{chrome} OPR/{version}.0.0.0",
        "Samsung Internet": f"{_WEBKIT} SamsungBrowser/{version}.0 Chrome/120.0.0.0"
        f"{mobile} Safari/537.36",
        "Firefox": f"Gecko/20100101 Firefox/{version}.0",
        "Safari": "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        f"Version/{version}.0{mobile} Safari/605.1.15",
    }
    return tokens.get(agent.browser, "")


def reduced_header(agent: Agent) -> str:
    """A stand-in header that keeps only family, major version and system."""
    system = _SYSTEM_TOKENS.get(agent.system, "unknown")
    return f"Mozilla/5.0 ({system}) {_browser_tokens(agent)}".rstrip()
