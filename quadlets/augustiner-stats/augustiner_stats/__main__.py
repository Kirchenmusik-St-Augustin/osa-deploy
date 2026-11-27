"""Command line: `python -m augustiner_stats run …` hourly, `… geodata …` monthly."""

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from augustiner_stats.geodata import GeodataError, update
from augustiner_stats.pipeline import Geodata, NotEnoughSpaceError, Settings, run

ZONE = "Europe/Vienna"
EXIT_FAILED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="augustiner_stats")
    commands = parser.add_subparsers(dest="command", required=True)
    hourly = commands.add_parser("run", help="read the log and render the dashboard")
    hourly.add_argument("--logs", type=Path, required=True)
    hourly.add_argument("--data", type=Path, required=True)
    hourly.add_argument("--www", type=Path, required=True)
    hourly.add_argument("--site", type=Path, required=True)
    hourly.add_argument(
        "--host", action="append", required=True, help="a name of the website"
    )
    hourly.add_argument("--min-free-mb", type=int, default=2048)
    monthly = commands.add_parser("geodata", help="fetch the DB-IP lists")
    monthly.add_argument("--data", type=Path, required=True)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    options = _parser().parse_args(arguments)
    zone = ZoneInfo(ZONE)
    now = datetime.now(zone)
    try:
        if options.command == "geodata":
            update(Path(options.data) / "geo", now.date())
            return 0
        settings = Settings(
            logs=options.logs,
            data=options.data,
            www=options.www,
            site=options.site,
            own_hosts=frozenset(host.lower() for host in options.host),
            zone=zone,
            min_free_mb=options.min_free_mb,
        )
        run(settings, now, Geodata.load)
    except (GeodataError, NotEnoughSpaceError) as error:
        return _failed(error)
    return 0


def _failed(error: Exception) -> int:
    """Logs the reason in one line; the cause is known, a traceback adds nothing."""
    logging.getLogger("augustiner_stats").error("%s", error)
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
