# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import partial
from typing import TypeVar

from clients.hyena import HyenaClient
from lib.cli import create_cli, run_app
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import gather_accs, parse_filter, short_addr, to_period_week
from strategy import StrategyConfig
from strategy.runner import close_all, print_positions, run_groups

T = TypeVar("T")
DD = defaultdict[str, defaultdict[str, T]]

GENESIS = datetime(2025, 12, 4, tzinfo=timezone.utc)
to_week = partial(to_period_week, genesis=GENESIS)


def _normalize_symbols(symbols: list[str]) -> list[str]:
    return [s if ":" in s else f"hyna:{s}" for s in symbols]


# MARK: Reports


async def print_info(accs: list[HyenaClient]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.0f}", total=sum),
        Column("P/Price", "{:,.4f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
    )

    async def row(acc: HyenaClient):
        await acc.warmup()
        p = await acc.profile() if await acc.registered() else None
        a = short_addr(acc.address)
        if not p:
            return ("✗", acc.name, a, 0, 0, 0, 0)
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance)

    for r in await gather_accs(accs, row):
        tbl.add_row(*r)

    tbl.print()


async def sync_fills(acc: HyenaClient, ttl: int) -> list[dict]:
    store_path = f".cache/hyena_{short_addr(acc.address)}_fills.pkl"
    store = DataStore(store_path, id_key="hash")
    await store.sync(acc.fetch_fills, ttl_sec=ttl)
    return store.get_all()


async def sync_rewards(acc: HyenaClient, ttl: int) -> list[dict]:
    store_path = f".cache/hyena_{short_addr(acc.address)}_rewards.pkl"
    store = DataStore(store_path, id_key="id")

    async def fetch(_since):
        rewards = await acc.rewards()
        return [
            {
                "id": h.id,
                "enaxPoints": h.enaxPoints,
                "period": to_week(
                    GENESIS + timedelta(weeks=int(h.id.removeprefix("reward-week-")) - 1)
                ),
            }
            for h in rewards.history
        ]

    await store.sync(fetch, ttl_sec=ttl)
    return store.get_all()


async def print_stats(
    accs: list[HyenaClient], period: str = "week", filter_period: str = "all", force: bool = False
):
    ttl = 0 if force else 3600
    fills_list, rewards_list = await asyncio.gather(
        gather_accs(accs, lambda acc: sync_fills(acc, ttl)),
        gather_accs(accs, lambda acc: sync_rewards(acc, ttl)),
    )

    gtrades: DD[list[dict]] = defaultdict(lambda: defaultdict(list))
    gpoints: DD[Decimal] = defaultdict(lambda: defaultdict(Decimal))

    for acc, fills in zip(accs, fills_list):
        for fill in fills:
            dt = datetime.fromtimestamp(fill["time"] / 1000, tz=timezone.utc)
            gtrades[to_week(dt)][acc.name].append(fill)

    for acc, rewards in zip(accs, rewards_list):
        for h in rewards:
            gpoints[h["period"]][acc.name] += Decimal(str(h["enaxPoints"]))

    all_periods = sorted(gtrades.keys() | gpoints.keys())
    periods_to_show = parse_filter(filter_period, all_periods)
    all_names = [acc.name for acc in accs]

    periods_data: dict[str, list[PeriodRow]] = {}
    for pk in all_periods:
        rows = []
        for name in all_names:
            fills = gtrades[pk].get(name, [])
            points = gpoints[pk].get(name, Decimal(0))
            if not fills and not points:
                continue
            vol = sum((Decimal(str(f["px"])) * Decimal(str(f["sz"])) for f in fills), Decimal(0))
            fee = sum((Decimal(str(f["fee"])) for f in fills), Decimal(0))
            pnl = sum((Decimal(str(f.get("closedPnl", 0))) for f in fills), Decimal(0))
            rows.append(PeriodRow(name, len(fills), vol, -pnl, points, fee))
        periods_data[pk] = rows

    render_stats(periods_data, periods_to_show, points_fmt="{:,.0f}", pprice_fmt="{:,.4f}")


# MARK: Main


async def main():
    cli = await create_cli("hyena", "configs/hyena.toml", ["privkey"])
    cfg = StrategyConfig.load(cli.config)
    cfg.symbols = _normalize_symbols(cfg.symbols)

    accs = [(HyenaClient.from_config(x), x.enabled) for x in cfg.accounts]
    all_accs, act_accs = [c for c, _ in accs], [c for c, e in accs if e]

    match cli.command:
        case "info":
            await print_info(all_accs)
        case "positions":
            await print_positions(act_accs)
        case "stats":
            await print_stats(all_accs, period=cli.group, filter_period=cli.filter, force=cli.force)
        case "close":
            await close_all(act_accs)
        case "trade":
            await run_groups(cfg, act_accs)


if __name__ == "__main__":
    run_app(main())
