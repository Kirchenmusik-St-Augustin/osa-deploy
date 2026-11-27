"""Render the visitor statistics of the website as static HTML pages.

Every chart is inline SVG or plain HTML: the pages carry no JavaScript, print
well and run under a strict content security policy. Hover texts use the
native SVG <title>; every chart also has a table with the exact figures.
"""

import html
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

_HERE = Path(__file__).parent
PAGE = (_HERE / "page.html").read_text(encoding="utf-8")
CSS = (_HERE / "dashboard.css").read_text(encoding="utf-8")
MAX_PARTS = 5
MAX_RANKED_ROWS = 10
OTHER_LABEL = "Sonstige"
HOME_SLUG = "index"
# The statistics timer runs a few minutes past every full hour.
HOURLY_NOTE = "wird einmal pro Stunde aktualisiert (kurz nach jeder vollen Stunde)"
FINAL_NOTE = "abgeschlossener Monat, die Zahlen ändern sich nicht mehr"
ARCHIVE_SLUG = "archiv"
WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
WEEKDAYS_LONG = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)
# Steps 150 to 650 of the sequential blue ramp, lightest for the fewest visitors.
HEAT_STEPS = ("#b6d5f8", "#84b9f3", "#519ced", "#257de0", "#1b64be", "#0f4a96")

KpiUnit = Literal["count", "seconds", "percent"]


@dataclass(frozen=True)
class Share:
    """One named part of a whole, or one row of a ranking."""

    label: str
    value: int


@dataclass(frozen=True)
class DomainEntry:
    """How many visits started on one domain, without and with "www."."""

    domain: str
    plain: int
    www: int

    @property
    def total(self) -> int:
        return self.plain + self.www


@dataclass(frozen=True)
class Kpi:
    """One headline figure with the value of the previous period."""

    label: str
    value: float
    previous: float | None
    unit: KpiUnit
    higher_is_better: bool
    note: str


@dataclass(frozen=True)
class Point:
    """One point of the visitor curve."""

    tick: str
    title: str
    value: int


@dataclass(frozen=True)
class Tab:
    """One period tab of the navigation."""

    slug: str
    label: str


@dataclass(frozen=True)
class PeriodReport:
    """Everything one period page shows."""

    slug: str
    heading: str
    range_label: str
    comparison_label: str
    curve_heading: str
    kpis: tuple[Kpi, ...]
    curve: tuple[Point, ...]
    devices: tuple[Share, ...]
    browsers: tuple[Share, ...]
    sources: tuple[Share, ...]
    languages: tuple[Share, ...]
    pages: tuple[Share, ...]
    entries: tuple[DomainEntry, ...]
    countries: tuple[Share, ...]
    programs: tuple[Share, ...]
    program_requests: tuple[Share, ...]
    week_hours: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class Site:
    """What every page shares: title, tabs, time of the data, preview flag."""

    title: str
    tabs: tuple[Tab, ...]
    generated: datetime
    preview: bool
    has_geodata: bool


# --- number formats --------------------------------------------------------


def format_count(value: float) -> str:
    """1234567 -> 1.234.567, the Austrian grouping."""
    return f"{round(value):,}".replace(",", ".")


def format_percent(part: float, whole: float) -> str:
    """Share as a whole percent; below one percent shows as "<1 %"."""
    if whole <= 0:
        return "0 %"
    share = 100 * part / whole
    if 0 < share < 1:
        return "<1 %"
    return f"{round(share)} %"


def format_seconds(seconds: float) -> str:
    """152 -> 2:32 min."""
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}:{rest:02d} min"


def format_kpi(value: float, unit: KpiUnit) -> str:
    if unit == "seconds":
        return format_seconds(value)
    if unit == "percent":
        return f"{round(value)} %"
    return format_count(value)


def nice_step(maximum: float, ticks: int = 4) -> int:
    """Round tick step (1, 2, 2.5, 5 or 10 times a power of ten), at least 1."""
    if maximum <= 0:
        return 1
    raw = maximum / ticks
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(f * magnitude for f in (1, 2, 2.5, 5, 10) if f * magnitude >= raw)
    return max(1, math.ceil(step))


def fold_parts(shares: Sequence[Share], limit: int = MAX_PARTS) -> list[Share]:
    """Keeps the largest parts and folds the rest into one "Sonstige" part."""
    ranked = sorted(shares, key=lambda share: share.value, reverse=True)
    if len(ranked) <= limit:
        return [share for share in ranked if share.value > 0]
    kept = ranked[: limit - 1]
    rest = sum(share.value for share in ranked[limit - 1 :])
    return [*kept, Share(OTHER_LABEL, rest)]


def esc(text: str) -> str:
    return html.escape(text, quote=True)


# --- building blocks -------------------------------------------------------


def kpi_tile(kpi: Kpi, comparison_label: str) -> str:
    delta = ""
    if kpi.previous:
        change = (kpi.value - kpi.previous) / kpi.previous * 100
        rounded = round(change)
        if rounded == 0:
            delta = f'<p class="delta">± 0 % {esc(comparison_label)}</p>'
        else:
            rising = rounded > 0
            good = rising == kpi.higher_is_better
            arrow = "▲" if rising else "▼"
            tone = "good" if good else "bad"
            delta = (
                f'<p class="delta {tone}"><span aria-hidden="true">{arrow}</span> '
                f"{abs(rounded)} % {esc(comparison_label)}</p>"
            )
    return (
        '<article class="tile">'
        f"<h3>{esc(kpi.label)}</h3>"
        f'<p class="value">{format_kpi(kpi.value, kpi.unit)}</p>'
        f"{delta}"
        f'<p class="note">{esc(kpi.note)}</p>'
        "</article>"
    )


def curve_chart(points: Sequence[Point], unit: str) -> str:
    """Line with a light area wash, one end label and a hover title per point.

    The plot stretches with the card while every text keeps its pixel size:
    horizontal positions are percentages, the line itself is a nested SVG
    drawn without aspect ratio and with a non-scaling stroke.
    """
    height, top = 200, 8
    plot_h = height - top
    peak = max((point.value for point in points), default=0)
    step = nice_step(peak)
    ceiling = step * max(1, math.ceil(peak / step))
    count = len(points)

    def x_pct(index: int) -> float:
        return 100 * index / (count - 1) if count > 1 else 50.0

    def y_px(value: float) -> float:
        return top + plot_h - plot_h * value / ceiling

    parts: list[str] = [
        (
            f'<div class="curve"><svg class="chart" width="100%" height="{height}" '
            f'role="img" aria-label="Verlauf der {esc(unit)}">'
        )
    ]
    for tick in range(0, ceiling + 1, step):
        y = y_px(tick)
        parts.append(
            f'<line class="grid" x1="0" x2="100%" y1="{y:.1f}" y2="{y:.1f}"/>'
            f'<text class="axis" x="0" dx="-8" y="{y + 4:.1f}" text-anchor="end">'
            f"{format_count(tick)}</text>"
        )
    label_every = max(1, math.ceil(count / 6))
    for index, point in enumerate(points):
        if index % label_every == 0:
            # Every second label gives way on a narrow card.
            css = "axis minor" if (index // label_every) % 2 else "axis"
            parts.append(
                f'<text class="{css}" x="{x_pct(index):.2f}%" y="{height + 18}" '
                f'text-anchor="middle">{esc(point.tick)}</text>'
            )
    if points:
        line = " ".join(
            f"{x_pct(i) * 10:.1f},{1000 - 1000 * point.value / ceiling:.1f}"
            for i, point in enumerate(points)
        )
        first, last = x_pct(0) * 10, x_pct(count - 1) * 10
        parts.append(
            f'<svg x="0" y="{top}" width="100%" height="{plot_h}" '
            'viewBox="0 0 1000 1000" preserveAspectRatio="none" overflow="visible">'
            f'<polygon class="wash" points="{first:.1f},1000 {line} '
            f'{last:.1f},1000"/><polyline class="line" points="{line}" '
            'vector-effect="non-scaling-stroke"/></svg>'
        )
        end_x, end_y = x_pct(count - 1), y_px(points[-1].value)
        parts.append(
            f'<circle class="dot" cx="{end_x:.2f}%" cy="{end_y:.1f}" r="4"/>'
            f'<text class="end-label" x="{end_x:.2f}%" dx="10" y="{end_y + 4:.1f}">'
            f"{format_count(points[-1].value)}</text>"
        )
        band = 100 / count
        for index, point in enumerate(points):
            start = max(0.0, x_pct(index) - band / 2)
            parts.append(
                f'<rect class="hit" x="{start:.2f}%" y="{top}" width="{band:.2f}%" '
                f'height="{plot_h}"><title>{esc(point.title)}: '
                f"{format_count(point.value)} {esc(unit)}</title></rect>"
            )
    parts.append(
        f'<line class="baseline" x1="0" x2="100%" y1="{top + plot_h}" '
        f'y2="{top + plot_h}"/></svg></div>'
    )
    return "".join(parts)


def donut_chart(shares: Sequence[Share], unit: str) -> str:
    """Ring of at most five parts, the total in the middle, a legend with figures."""
    parts = fold_parts(shares)
    total = sum(share.value for share in parts)
    size, radius, ring = 180, 66, 26
    centre = size / 2
    svg: list[str] = [
        (
            f'<svg class="donut" viewBox="0 0 {size} {size}" role="img" '
            f'aria-label="Aufteilung der {esc(unit)}">'
        )
    ]
    offset = 0.0
    boundaries: list[float] = []
    for slot, share in enumerate(parts, start=1):
        fraction = share.value / total if total else 0
        svg.append(
            f'<circle class="slot-{slot}" cx="{centre}" cy="{centre}" r="{radius}" '
            f'pathLength="1000" stroke-width="{ring}" '
            f'stroke-dasharray="{fraction * 1000:.2f} 1000" '
            f'stroke-dashoffset="{-offset * 1000:.2f}" '
            f'transform="rotate(-90 {centre} {centre})">'
            f"<title>{esc(share.label)}: {format_count(share.value)} {esc(unit)} "
            f"({format_percent(share.value, total)})</title></circle>"
        )
        offset += fraction
        boundaries.append(offset)
    if len(parts) > 1:
        for boundary in [0.0, *boundaries[:-1]]:
            angle = 2 * math.pi * boundary - math.pi / 2
            inner, outer = radius - ring / 2 - 1, radius + ring / 2 + 1
            svg.append(
                f'<line class="gap" x1="{centre + inner * math.cos(angle):.2f}" '
                f'y1="{centre + inner * math.sin(angle):.2f}" '
                f'x2="{centre + outer * math.cos(angle):.2f}" '
                f'y2="{centre + outer * math.sin(angle):.2f}"/>'
            )
    svg.append(
        f'<text class="donut-total" x="{centre}" y="{centre + 2}" text-anchor="middle">'
        f"{format_count(total)}</text>"
        f'<text class="donut-unit" x="{centre}" y="{centre + 20}" text-anchor="middle">'
        f"{esc(unit)}</text></svg>"
    )
    legend = "".join(
        f'<li><span class="swatch slot-{slot}" aria-hidden="true"></span>'
        f'<span class="name">{esc(share.label)}</span>'
        f'<span class="share">{format_percent(share.value, total)}</span>'
        f'<span class="count">{format_count(share.value)}</span></li>'
        for slot, share in enumerate(parts, start=1)
    )
    return (
        f'<div class="donut-wrap">{"".join(svg)}<ul class="legend">{legend}</ul></div>'
    )


def ranked_bars(shares: Sequence[Share], unit: str) -> str:
    """Largest first, one hue, value at the tip of each bar."""
    rows = sorted(shares, key=lambda share: share.value, reverse=True)[:MAX_RANKED_ROWS]
    peak = max((share.value for share in rows), default=0) or 1
    total = sum(share.value for share in shares)
    items = "".join(
        f'<li title="{esc(share.label)}: {format_count(share.value)} {esc(unit)} '
        f'({format_percent(share.value, total)})">'
        f'<span class="name">{esc(share.label)}</span>'
        '<span class="track"><span class="bar" '
        f'style="--w:{100 * share.value / peak:.1f}%"></span></span>'
        f'<span class="count">{format_count(share.value)}</span></li>'
        for share in rows
    )
    return f'<ol class="ranked">{items}</ol>'


def heatmap(week_hours: Sequence[Sequence[int]]) -> str:
    """Weekday rows by hour columns, darker where more visitors came."""
    peak = max((max(row) for row in week_hours), default=0) or 1
    cells: list[str] = []
    for day, row in enumerate(week_hours):
        cells.append(f'<span class="day">{WEEKDAYS[day]}</span>')
        for hour, value in enumerate(row):
            level = min(len(HEAT_STEPS) - 1, math.floor(value / peak * len(HEAT_STEPS)))
            css = f"h{level}" if value else "h-empty"
            cells.append(
                f'<span class="cell {css}" title="{WEEKDAYS_LONG[day]}, '
                f'{hour}–{hour + 1} Uhr: {format_count(value)} Besucher"></span>'
            )
    cells.append('<span class="day"></span>')
    cells.extend(
        f'<span class="hour">{hour if hour % 3 == 0 else ""}</span>'
        for hour in range(24)
    )
    scale = "".join(
        f'<span class="h{level}"></span>' for level in range(len(HEAT_STEPS))
    )
    return (
        '<div class="heat" role="img" '
        'aria-label="Besucher nach Wochentag und Uhrzeit">'
        f"{''.join(cells)}</div>"
        '<p class="scale"><span>wenige</span>'
        f'<span class="scale-steps" aria-hidden="true">{scale}</span>'
        "<span>viele Besucher</span></p>"
    )


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    head = "".join(f"<th>{esc(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return (
        '<details class="data"><summary>Zahlen als Tabelle</summary>'
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></details>"
    )


def share_table(label: str, shares: Sequence[Share]) -> str:
    total = sum(share.value for share in shares)
    ranked = sorted(shares, key=lambda share: share.value, reverse=True)
    return table(
        (label, "Anzahl", "Anteil"),
        [
            (s.label, format_count(s.value), format_percent(s.value, total))
            for s in ranked
        ],
    )


def program_table(programs: Sequence[Share], requests: Sequence[Share]) -> str:
    """Programs by visits, the requests they made only as a second column."""
    asked = {share.label: share.value for share in requests}
    total = sum(share.value for share in programs)
    ranked = sorted(programs, key=lambda share: share.value, reverse=True)
    return table(
        ("Art", "Besuche", "Anfragen", "Anteil der Besuche"),
        [
            (
                s.label,
                format_count(s.value),
                format_count(asked.get(s.label, 0)),
                format_percent(s.value, total),
            )
            for s in ranked
        ],
    )


def domain_table(entries: Sequence[DomainEntry]) -> str:
    """Always visible: every domain once, split into without and with "www."."""
    ranked = sorted(entries, key=lambda entry: entry.total, reverse=True)
    total = sum(entry.total for entry in ranked)
    rows = "".join(
        f"<tr><td>{esc(entry.domain)}</td><td>{format_count(entry.plain)}</td>"
        f"<td>{format_count(entry.www)}</td><td>{format_count(entry.total)}</td>"
        f"<td>{format_percent(entry.total, total)}</td></tr>"
        for entry in ranked
    )
    plain = sum(entry.plain for entry in ranked)
    www = sum(entry.www for entry in ranked)
    return (
        '<div class="scroll"><table class="domains"><thead><tr><th>Adresse</th>'
        "<th>ohne www</th>"
        "<th>mit www</th><th>zusammen</th><th>Anteil</th></tr></thead>"
        f"<tbody>{rows}</tbody><tfoot><tr><td>Gesamt</td><td>{format_count(plain)}</td>"
        f"<td>{format_count(www)}</td><td>{format_count(total)}</td><td>100\u202f%</td>"
        "</tr></tfoot></table></div>"
    )


def figure(title: str, body: str) -> str:
    """One of several charts inside a card, with its own small heading."""
    return f'<div class="figure"><h3>{esc(title)}</h3>{body}</div>'


def card(
    title: str, explanation: str, body: str, *, wide: bool = False, anchor: str = ""
) -> str:
    css = "card wide" if wide else "card"
    anchor_attr = f' id="{esc(anchor)}"' if anchor else ""
    return (
        f'<section class="{css}"{anchor_attr}><h2>{esc(title)}</h2>{body}'
        f'<p class="explain">{esc(explanation)}</p></section>'
    )


# --- page ------------------------------------------------------------------


def page_href(slug: str) -> str:
    """Root-relative link, so every page links alike, archived months included."""
    return "/" if slug == HOME_SLUG else f"/{esc(slug)}.html"


def navigation(site: Site, current: str) -> str:
    return "".join(
        f'<a href="{page_href(tab.slug)}"'
        + (' aria-current="page"' if tab.slug == current else "")
        + f">{esc(tab.label)}</a>"
        for tab in site.tabs
    )


def preview_banner(site: Site) -> str:
    if not site.preview:
        return ""
    return (
        '<p class="preview">Vorschau mit erfundenen Testdaten – noch keine echten '
        "Zahlen.</p>"
    )


def render_period(site: Site, report: PeriodReport, *, archived: bool = False) -> str:
    """One period page; an archived month marks the archive tab as current."""
    tiles = (
        '<section class="tiles" aria-label="Kennzahlen">'
        + "".join(kpi_tile(kpi, report.comparison_label) for kpi in report.kpis)
        + "</section>"
    )
    hour_rows = [
        (WEEKDAYS_LONG[day], *(format_count(value) for value in row))
        for day, row in enumerate(report.week_hours)
    ]
    sections = [
        card(
            report.curve_heading,
            "Ein Besucher ist ein Gerät: Wer am Handy und am Computer schaut, "
            "zählt zweimal.",
            curve_chart(report.curve, "Besucher")
            + table(
                ("Zeitpunkt", "Besucher"),
                [(point.title, format_count(point.value)) for point in report.curve],
            ),
            wide=True,
        ),
        card(
            "Womit wird gelesen?",
            "Erkannt an der Kennung, die jeder Browser bei einem Aufruf mitschickt.",
            '<div class="pair">'
            + figure(
                "Gerät",
                donut_chart(report.devices, "Besucher")
                + share_table("Gerät", report.devices),
            )
            + figure(
                "Browser",
                donut_chart(report.browsers, "Besucher")
                + share_table("Browser", report.browsers),
            )
            + "</div>",
            wide=True,
        ),
        card(
            "Wie kommen die Besucher?",
            "„Direkt“ heißt: Adresse eingetippt, Lesezeichen oder ein Link aus "
            "einer E-Mail oder App.",
            donut_chart(report.sources, "Besuche")
            + share_table("Herkunft", report.sources),
        ),
        card(
            "In welcher Sprache?",
            "Welche Sprachfassung der Seiten gelesen wurde.",
            donut_chart(report.languages, "Aufrufe")
            + share_table("Sprache", report.languages),
        ),
        card(
            "Beliebteste Seiten",
            "Wie oft jede Seite aufgerufen wurde.",
            ranked_bars(report.pages, "Aufrufe") + share_table("Seite", report.pages),
        ),
    ]
    if site.has_geodata:
        sections.append(
            card(
                "Aus welchen Ländern?",
                "Ungefährer Standort, ermittelt aus dem Netz des Internetanbieters.",
                ranked_bars(report.countries, "Besucher")
                + share_table("Land", report.countries),
            )
        )
    sections += [
        card(
            "Über welche Adresse kamen die Besucher?",
            "Gezählt wird der erste Aufruf jedes Besuchs. Die Kurzadressen leiten "
            "auf die passende Seite von augustinerkirche.at weiter.",
            ranked_bars(
                [Share(entry.domain, entry.total) for entry in report.entries],
                "Besuche",
            )
            + domain_table(report.entries),
            wide=True,
        ),
        card(
            "Wann wird gelesen?",
            "Je dunkler das Feld, desto mehr Besucher kamen zu dieser Stunde.",
            heatmap(report.week_hours)
            + table(("Wochentag", *(f"{hour} Uhr" for hour in range(24))), hour_rows),
            wide=True,
        ),
    ]
    sections.append(
        card(
            "Automatische Zugriffe (nicht mitgezählt)",
            PROGRAMS_NOTE,
            ranked_bars(report.programs, "Besuche")
            + program_table(report.programs, report.program_requests),
            wide=True,
            anchor="automatisch",
        )
    )
    current = ARCHIVE_SLUG if archived else report.slug
    back = (
        f'<p class="back"><a href="{page_href(ARCHIVE_SLUG)}">'
        "← Zurück zum Archiv</a></p>"
        if archived
        else ""
    )
    return PAGE.format(
        title=esc(f"{report.heading} – {site.title}"),
        css=CSS,
        banner=preview_banner(site),
        site_title=esc(site.title),
        nav=navigation(site, current),
        back=back,
        scope=SCOPE_NOTE,
        heading=esc(report.heading),
        range_label=esc(report.range_label),
        generated=site.generated.strftime("%d.%m.%Y, %H:%M"),
        freshness=FINAL_NOTE if archived else HOURLY_NOTE,
        tiles=tiles,
        sections="".join(sections),
        geodata=(
            ' Länder: <a href="https://db-ip.com">IP Geolocation by DB-IP</a> '
            "(CC BY 4.0)."
            if site.has_geodata
            else ""
        ),
    )


def render_archive(site: Site, months: Sequence[Tab]) -> str:
    """Index of the finished months, newest first."""
    items = (
        "".join(
            f'<li><a href="{page_href(f"{ARCHIVE_SLUG}/{month.slug}")}">'
            f"{esc(month.label)}</a></li>"
            for month in months
        )
        or "<li>Noch kein Monat abgeschlossen.</li>"
    )
    return PAGE.format(
        title=esc(f"Archiv – {site.title}"),
        css=CSS,
        banner=preview_banner(site),
        site_title=esc(site.title),
        nav=navigation(site, ARCHIVE_SLUG),
        back="",
        scope="",
        heading="Archiv",
        range_label="Jeder abgeschlossene Monat bleibt hier als eigene Seite erhalten",
        generated=site.generated.strftime("%d.%m.%Y, %H:%M"),
        freshness=HOURLY_NOTE,
        tiles="",
        sections=f'<section class="card wide"><h2>Monatsberichte</h2>'
        f'<ul class="months">{items}</ul></section>',
        geodata="",
    )


SCOPE_NOTE = (
    '<p class="scope"><span><strong>Alle Zahlen auf dieser Seite zählen nur '
    "Menschen.</strong> "
    "Suchmaschinen, KI-Dienste und andere Programme sind in keiner "
    "anderen Kachel, Grafik oder Tabelle enthalten – sie stehen getrennt "
    '<a href="#automatisch">in der letzten Kachel (ganz unten)</a>.</span></p>'
)

PROGRAMS_NOTE = (
    "Programme statt Menschen: Suchmaschinen, KI-Dienste, Link-Vorschauen in "
    "Messengern und Programme, die Websites nach Sicherheitslücken absuchen – "
    "solche Suchläufe treffen jede Website im Internet, ständig und automatisch. "
    "Jedes Programm zählt einmal pro Tag als Besuch, egal wie viele Seiten es "
    "abruft. Diese Zugriffe sind in keiner anderen Zahl dieser Seite enthalten."
)
