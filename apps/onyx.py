# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from functools import partial
from typing import TypeVar

from clients.onyx import OnyxClient
from lib.cli import create_cli, run_app
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import gather_accs, parse_filter, short_addr, to_period_day, to_period_week
from strategy import StrategyConfig
from strategy.runner import close_all, print_positions, run_groups

T = TypeVar("T")
DD = defaultdict[str, defaultdict[str, T]]

GENESIS = datetime(2026, 3, 1, tzinfo=timezone.utc)
to_week = partial(to_period_week, genesis=GENESIS)


# MARK: Storages


async def sync_fills(acc: OnyxClient, ttl: int) -> list[dict]:
    store_path = f".cache/onyx_{short_addr(acc.address)}_fills.pkl"
    store = DataStore(store_path, id_key="hash")
    await store.sync(acc.fetch_fills, ttl_sec=ttl)
    return store.get_all()


# MARK: Reports


async def print_info(accs: list[OnyxClient]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.1f}", total=sum),
        Column("P/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
    )

    async def row(acc: OnyxClient):
        await acc.warmup()
        p = await acc.profile() if await acc.registered() else None
        a = short_addr(acc.address)
        if not p:
            return ("✗", acc.name, a, 0, 0, 0, 0)
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance)

    for r in await gather_accs(accs, row):
        tbl.add_row(*r)

    tbl.print()


async def print_stats(
    accs: list[OnyxClient], period: str = "week", filter_period: str = "all", force: bool = False
):
    ttl = 0 if force else 3600
    fills_list = await gather_accs(accs, lambda acc: sync_fills(acc, ttl))

    period_fn = to_period_day if period == "day" else to_week
    gtrades: DD[list[dict]] = defaultdict(lambda: defaultdict(list))

    for acc, fills in zip(accs, fills_list):
        for fill in fills:
            dt = datetime.fromtimestamp(fill["time"] / 1000, tz=timezone.utc)
            if dt < GENESIS:
                continue
            gtrades[period_fn(dt)][acc.name].append(fill)

    all_periods = sorted(gtrades.keys())
    periods_to_show = parse_filter(filter_period, all_periods)
    all_names = [acc.name for acc in accs]

    periods_data: dict[str, list[PeriodRow]] = {}
    for pk in all_periods:
        rows = []
        for name in all_names:
            fills = gtrades[pk].get(name, [])
            if not fills:
                continue
            vol = sum((Decimal(str(f["px"])) * Decimal(str(f["sz"])) for f in fills), Decimal(0))
            fee = sum((Decimal(str(f["fee"])) for f in fills), Decimal(0))
            pnl = sum((Decimal(str(f.get("closedPnl", 0))) for f in fills), Decimal(0))
            rows.append(PeriodRow(name, len(fills), vol, -pnl, Decimal(0), fee))
        periods_data[pk] = rows

    render_stats(periods_data, periods_to_show, pprice_fmt="{:,.2f}")


# MARK: Main


async def main():
    cli = await create_cli("onyx", "configs/onyx.toml", ["privkey"])
    cfg = StrategyConfig.load(cli.config)

    accs = [(OnyxClient.from_config(x), x.enabled) for x in cfg.accounts]
    all_accs, act_accs = [c for c, _ in accs], [c for c, e in accs if e]
    for c in act_accs:
        c._symbols = cfg.symbols

    match cli.command:
        case "info":
            await print_info(all_accs)
        case "positions":
            await print_positions(act_accs)
        case "close":
            await close_all(act_accs)
        case "stats":
            await print_stats(all_accs, period=cli.group, filter_period=cli.filter, force=cli.force)
        case "trade":
            await run_groups(cfg, act_accs)


if __name__ == "__main__":
    run_app(main())
