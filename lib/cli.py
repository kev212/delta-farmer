# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | It's not a bug, it's undocumented behavior
import argparse
import asyncio
import glob
import os
import re
import sys
from collections.abc import Coroutine

from . import telemetry
from .crypto import config_cli_parser
from .http import FatalError
from .logger import logger


def _get_version() -> str:
    try:
        pyproject = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
        with open(pyproject) as f:
            match = re.search(r'version\s*=\s*"([^"]+)"', f.read())
        return f"v{match.group(1)} " if match else ""
    except Exception:
        return ""


VERSION = _get_version()


def print_header():
    print(
        f":: delta-farmer {VERSION}| https://x.com/uid127 | https://t.me/eazyrekt", file=sys.stderr
    )


def create_cli(name: str, config_path: str, sec_fields: list[str]) -> argparse.Namespace:
    print_header()

    cli = argparse.ArgumentParser(prog=name)
    cli.add_argument("-c", "--config", default=config_path, help="Path to config file")

    sub = cli.add_subparsers(dest="command")
    sub.add_parser("trade", help="Run trading manager")
    sub.add_parser("close", help="Close all positions")
    sub.add_parser("info", help="Show accounts info")
    sub.add_parser("clean", help="Delete cached data")

    stats_parser = sub.add_parser("stats", help="Show trading stats")
    stats_parser.add_argument(
        "filter", nargs="?", default="all", help="Period filter (all/this/last/W05)"
    )
    stats_parser.add_argument("-g", "--group", choices=["week", "day"], default="week")
    stats_parser.add_argument("--force", dest="force", action="store_true", help="Force stats sync")
    stats_parser.add_argument("--sync", dest="force", action="store_true", help=argparse.SUPPRESS)

    handle_config = config_cli_parser(sub, fields=sec_fields)
    args = cli.parse_args()

    telemetry.init(exchange=name, command=args.command or "", version=VERSION)
    telemetry.track("$pageview", {"$current_url": f"cli://delta-farmer/{name}/{args.command}"})

    if args.command is None:
        cli.print_help()
        exit(1)

    if args.command == "config":
        handle_config(args)
        exit(0)

    if args.command == "clean":
        files = glob.glob(f".cache/{name}_*.pkl")
        for f in files:
            os.remove(f)
            print(f"Deleted {f}", file=sys.stderr)
        if not files:
            print("No cache files found", file=sys.stderr)
        exit(0)

    return args


def run_app(coro: Coroutine) -> None:
    try:
        asyncio.run(coro)
    except FatalError as e:
        logger.error(str(e))
    except KeyboardInterrupt:
        logger.info("Interrupted — closing all positions...")
