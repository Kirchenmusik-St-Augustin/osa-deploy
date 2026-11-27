"""Tell people from programs, one client and one day at a time.

A client is an address with one User-Agent header. The first rule that fits
decides, and its verdict covers every request of that client on that day: a
scanner that also fetched the start page is still a scanner.
"""

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import timedelta
from enum import Enum

from augustiner_stats.accesslog import Request
from augustiner_stats.pages import is_page_view


class Category(Enum):
    """What kind of program a client is; the value is the label of the reports."""

    SCANNER = "Suche nach Sicherheitslücken"
    SEARCH = "Suchmaschinen"
    AI = "KI-Dienste"
    PREVIEW = "Link-Vorschauen (WhatsApp & Co.)"
    SEO = "SEO- und Überwachungsdienste"
    TOOL = "Werkzeuge und Skripte"
    NO_SCRIPT = "Ohne JavaScript (Programme)"
    RAPID = "Auffällig schnell (Programme)"
    DATACENTER = "Aus Rechenzentren (Programme)"

    @property
    def token(self) -> str:
        """Single word for the remote-user field of the GoAccess lines."""
        return _TOKENS[self]


_TOKENS = {
    Category.SCANNER: "Scanner",
    Category.SEARCH: "Suchmaschine",
    Category.AI: "KI-Dienst",
    Category.PREVIEW: "Link-Vorschau",
    Category.SEO: "SEO/Überwachung",
    Category.TOOL: "Werkzeug/Skript",
    Category.NO_SCRIPT: "Ohne-JavaScript",
    Category.RAPID: "Zu-schnell",
    Category.DATACENTER: "Rechenzentrum",
}

# Paths no visitor of a static church website asks for. The site's own Caddy
# allowlist already answers them with 404; here they mark the asker.
SCANNER_PATH = re.compile(
    r"(?i)(?:^|/)(?:wp-|wordpress|xmlrpc\.php|phpmyadmin|pma/|cgi-bin|actuator"
    r"|vendor/|\.env|\.git|\.svn|\.aws|\.ds_store|config\.(?:php|json|ya?ml)"
    r"|admin\.php|setup\.php|server-status|boaform|hnap1|owa/)"
    r"|\.(?:php\d?|aspx?|jsp|cgi|sh|bak|sql|ini|log)$"
)
DISTINCT_NOT_FOUND_LIMIT = 5
READ_METHODS = frozenset({"GET", "HEAD"})

SEARCH_AGENT = re.compile(
    r"(?i)googlebot|google-inspectiontool|bingbot|bingpreview|applebot|duckduckbot"
    r"|yandex(?:bot|images)|baiduspider|seznambot|qwantbot|ecosia|mojeekbot"
    r"|petalbot|sogou|exabot|yahoo! slurp|naver"
)
AI_AGENT = re.compile(
    r"(?i)gptbot|chatgpt-user|oai-searchbot|claudebot|claude-user|claude-searchbot"
    r"|anthropic-ai|perplexitybot|perplexity-user|google-extended|ccbot|bytespider"
    r"|amazonbot|meta-externalagent|meta-externalfetcher|cohere-ai|diffbot"
    r"|youbot|mistralai-user|ai2bot|timpibot|omgili|imagesiftbot"
)
PREVIEW_AGENT = re.compile(
    r"(?i)whatsapp|facebookexternalhit|facebookcatalog|telegrambot|slackbot"
    r"|discordbot|twitterbot|linkedinbot|skypeuripreview|signal|viber|pinterest"
    r"|redditbot|mastodon|iframely|embedly"
)
SEO_AGENT = re.compile(
    r"(?i)ahrefsbot|semrushbot|mj12bot|dotbot|rogerbot|screaming frog|serpstatbot"
    r"|dataforseobot|blexbot|barkrowler|seokicks|uptimerobot|pingdom|statuscake"
    r"|site24x7|betteruptime|uptime-kuma|freshping|monitor|checkly"
)
TOOL_AGENT = re.compile(
    r"(?i)curl/|wget/|python-requests|python-urllib|aiohttp|httpx|go-http-client"
    r"|java/|okhttp|axios|node-fetch|undici|libwww|lwp::|scrapy|headlesschrome"
    r"|phantomjs|puppeteer|playwright|selenium|zgrab|masscan|nmap|nuclei|httpclient"
    r"|\bbot\b|crawler|spider|scanner"
)
AGENT_RULES: tuple[tuple[Category, re.Pattern[str]], ...] = (
    (Category.SEARCH, SEARCH_AGENT),
    (Category.AI, AI_AGENT),
    (Category.PREVIEW, PREVIEW_AGENT),
    (Category.SEO, SEO_AGENT),
    (Category.TOOL, TOOL_AGENT),
)
SHORTEST_BROWSER_AGENT = 20

# main.js of the website fetches this file on every page, so a browser that
# runs the page's script asks for it each time (it is served with no-cache).
SCRIPT_SIGNAL = "/data/navigation.json"
ROBOT_FILES = frozenset({"/robots.txt", "/sitemap.xml"})
RAPID_PAGES_PER_MINUTE = 30
MOST_PAGES_PER_DAY = 300
OLD_PROTOCOL = "HTTP/1.0"

# Hosting and cloud networks people do not browse from: a fixed list of the
# large ones plus every network whose operator's name speaks of hosting.
# English terms only, so a German "Rechenzentrum" of an employer stays a
# workplace. iCloud Private Relay leaves through Cloudflare and Akamai, and
# Fastly carries relayed traffic as well: those networks carry real visitors
# and never count as hosting, whatever their names say.
DATACENTER_ASNS = frozenset(
    {
        24940,  # Hetzner
        16276,  # OVH
        14061,  # DigitalOcean
        16509,  # Amazon AWS
        14618,  # Amazon AWS
        8075,  # Microsoft Azure
        396982,  # Google Cloud
        15169,  # Google
        45102,  # Alibaba Cloud
        132203,  # Tencent Cloud
        63949,  # Akamai Linode
        51167,  # Contabo
        197540,  # netcup
        20473,  # Vultr (Choopa)
        12876,  # Scaleway
        9009,  # M247
        212238,  # Datacamp / CDN77
        60781,  # LeaseWeb
        28753,  # LeaseWeb
        8100,  # QuadraNet
        53667,  # FranTech / BuyVM
        31898,  # Oracle Cloud
        135377,  # UCloud
        46606,  # Unified Layer
        36352,  # ColoCrossing
        207990,  # HostRoyale (proxy exits seen on the development stage)
        203020,  # HostRoyale
        64286,  # LogicWeb
        18779,  # EGIHosting
    }
)
RELAY_ASNS = frozenset({13335, 14789, 209242, 36183, 54113})
HOSTING_NAME = re.compile(
    r"(?i)hosting|\bhost\b|^host|server|\bvps\b|datacenter|data cent(?:er|re)"
    r"|colocation|cloud|dedicated|virtual private"
)


def is_hosting_network(asn: int, operator: str) -> bool:
    """A network people do not browse from, by number or by its operator's name."""
    if asn in RELAY_ASNS:
        return False
    return asn in DATACENTER_ASNS or HOSTING_NAME.search(operator) is not None


ClientKey = tuple[str, str]


def client_key(request: Request) -> ClientKey:
    return request.ip, request.user_agent


def _scans(requests: Sequence[Request]) -> bool:
    if any(request.method not in READ_METHODS for request in requests):
        return True
    if any(SCANNER_PATH.search(request.path) for request in requests):
        return True
    missing = {request.path for request in requests if request.status == 404}
    return len(missing) >= DISTINCT_NOT_FOUND_LIMIT


def _agent_category(agent: str) -> Category | None:
    if len(agent) < SHORTEST_BROWSER_AGENT:
        return Category.TOOL
    return next(
        (category for category, rule in AGENT_RULES if rule.search(agent)), None
    )


def _too_fast(requests: Sequence[Request]) -> bool:
    views = [request.time for request in requests if is_page_view(request)]
    if len(views) > MOST_PAGES_PER_DAY:
        return True
    window = timedelta(minutes=1)
    first = 0
    for last, moment in enumerate(views):
        while moment - views[first] > window:
            first += 1
        if last - first + 1 > RAPID_PAGES_PER_MINUTE:
            return True
    return False


def _behaves_like_program(requests: Sequence[Request]) -> bool:
    if any(request.path in ROBOT_FILES for request in requests):
        return True
    if all(request.method == "HEAD" for request in requests):
        return True
    return any(request.protocol == OLD_PROTOCOL for request in requests)


def classify_client(
    requests: Sequence[Request], is_datacenter: Callable[[str], bool]
) -> Category | None:
    """The category of one client's requests on one day, None for a person."""
    if _scans(requests):
        return Category.SCANNER
    agent = _agent_category(requests[0].user_agent)
    if agent is not None:
        return agent
    if not any(request.path.endswith(SCRIPT_SIGNAL) for request in requests):
        return Category.NO_SCRIPT
    if _too_fast(requests) or _behaves_like_program(requests):
        return Category.RAPID
    if is_datacenter(requests[0].ip):
        return Category.DATACENTER
    return None


def group_by_client(requests: Iterable[Request]) -> dict[ClientKey, list[Request]]:
    clients: dict[ClientKey, list[Request]] = defaultdict(list)
    for request in requests:
        clients[client_key(request)].append(request)
    return dict(clients)
