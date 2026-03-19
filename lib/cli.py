# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | It's not a bug, it's undocumented behavior
import argparse
import asyncio
import glob
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Coroutine

from pydantic import BaseModel, Field

from . import telegram as tg
from . import telemetry
from .crypto import config_cli_parser
from .http import FatalError
from .logger import logger
from .telegram import TgConfig


def eprint(*args, **kwargs):
    print(*args, **kwargs, file=sys.stderr)


class HelpFormatter(argparse.HelpFormatter):
    def _iter_indented_subactions(self, action):
        for subaction in super()._iter_indented_subactions(action):
            if getattr(subaction, "help", None) == argparse.SUPPRESS:
                continue
            yield subaction


def _get_version() -> tuple[str, bool]:
    try:
        pyproject = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")
        with open(pyproject) as f:
            match = re.search(r'version\s*=\s*"([^"]+)"', f.read())
        version = match.group(1) if match else None
        if not version:
            return "", True

        repo = os.path.join(os.path.dirname(__file__), "..")
        try:
            subprocess.check_output(
                ["git", "describe", "--exact-match", "--tags", "HEAD"],
                cwd=repo,
                stderr=subprocess.DEVNULL,
            )
            return f"v{version} ", True
        except subprocess.CalledProcessError:
            short = (
                subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"], cwd=repo, stderr=subprocess.DEVNULL
                )
                .decode()
                .strip()
            )
            return f"v{version}-{short} ", False
    except Exception:
        return "", True


VERSION, IS_RELEASE = _get_version()


class _TgOnlyConfig(BaseModel):
    telegram: TgConfig = Field(default_factory=TgConfig)


def _load_tg_config(filepath: str) -> TgConfig:
    try:
        with open(filepath, "rb") as fp:
            obj = tomllib.load(fp)
        return _TgOnlyConfig.model_validate(obj).telegram
    except Exception:
        return TgConfig()


async def _handle_tgtest(name: str) -> None:
    if not tg.enabled():
        eprint("Telegram not configured (set token and chat_id in [telegram] section)")
        sys.exit(1)

    await tg.send(f"✅ *{name}* — Telegram is working")
    eprint("Message sent.")


async def create_cli(name: str, config_path: str, sec_fields: list[str]) -> argparse.Namespace:
    eprint(f":: delta-farmer {VERSION}| https://x.com/uid127 | https://t.me/eazyrekt")

    cli = argparse.ArgumentParser(prog=name, formatter_class=HelpFormatter)
    cli.add_argument("-c", "--config", default=config_path, help="Path to config file")

    sub = cli.add_subparsers(dest="command")

    def _sub(name: str, **kw) -> argparse.ArgumentParser:
        p = sub.add_parser(name, **kw)
        p.add_argument("-c", "--config", default=argparse.SUPPRESS, help="Path to config file")
        return p

    _sub("trade", help="Run trading manager")
    _sub("close", help="Close all positions")
    _sub("positions", help="Show active positions")
    _sub("info", help="Show accounts info")
    _sub("clean", help="Delete cached data")
    _sub("tgtest", help=argparse.SUPPRESS)

    stats_parser = _sub("stats", help="Show trading stats")
    stats_parser.add_argument(
        "filter", nargs="?", default="all", help="Period filter (all/this/last/W05)"
    )
    stats_parser.add_argument("-g", "--group", choices=["week", "day"], default="week")
    stats_parser.add_argument("--force", dest="force", action="store_true", help="Force stats sync")
    stats_parser.add_argument("--sync", dest="force", action="store_true", help=argparse.SUPPRESS)

    all_fields = list(sec_fields) + ([] if "token" in sec_fields else ["token"])
    handle_config = config_cli_parser(sub, fields=all_fields)
    sub.metavar = (
        "{"
        + ",".join(
            action.dest
            for action in sub._get_subactions()
            if getattr(action, "help", None) != argparse.SUPPRESS
        )
        + "}"
    )
    args = cli.parse_args()

    telemetry.init(exchange=name, command=args.command or "", version=VERSION, release=IS_RELEASE)
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
            eprint(f"Deleted {f}")
        if not files:
            eprint("No cache files found")
        exit(0)

    if args.command in ("trade", "tgtest"):
        tg.init(name, _load_tg_config(args.config))

    if args.command == "tgtest":
        await _handle_tgtest(name)
        sys.exit(0)

    return args


def run_app(coro: Coroutine) -> None:
    try:
        asyncio.run(coro)
    except FatalError as e:
        logger.error(str(e))
    except KeyboardInterrupt:
        pass
