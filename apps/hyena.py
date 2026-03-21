# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import partial

from clients.hyena import HyenaClient
from lib.cli import create_cli, run_app
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import gather_accs, parse_filter, short_addr, to_period_week
from strategy import StrategyConfig
from strategy.runner import close_all, print_positions, run_groups

GENESIS = datetime(2025, 12, 4, tzinfo=timezone.utc)
to_week = partial(to_period_week, genesis=GENESIS)


# MARK: Reports


async def print_info(accs: list[HyenaClient]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Balance", "{:,.2f}", total=sum),
        Column("enaxPts", "{:,.0f}", total=sum),
        Column("Sats", "{:,.0f}", total=sum),
        Column("Rank", justify="left"),
        Column("Claimable", "{:,.2f}", total=sum),
    )

    async def row(acc: HyenaClient):
        await acc.warmup()
        bal, rewards = await asyncio.gather(acc.balance(), acc.rewards())
        addr = short_addr(acc.address)
        r = rewards.rank
        return (
            "✓",
            acc.name,
            addr,
            bal,
            rewards.balance.enaxPoints,
            rewards.balance.sats,
            f"{r.tier} ({r.percentile:.1f}%)",
            rewards.availableToClaim,
        )

    for r in await gather_accs(accs, row):
        tbl.add_row(*r)

    tbl.print()


async def _fetch_fills(acc: HyenaClient) -> list[dict]:
    rep = await acc.http.request(
        "POST", "/info", json={"type": "userFills", "user": acc.address, "aggregateByTime": True}
    )
    return rep.json() if rep.ok else []


async def print_stats(accs: list[HyenaClient], period: str = "week", filter_period: str = "all"):
    fills_list, rewards_list = await asyncio.gather(
        gather_accs(accs, _fetch_fills),
        gather_accs(accs, lambda acc: acc.rewards()),
    )

    gtrades: defaultdict[str, defaultdict[str, list]] = defaultdict(lambda: defaultdict(list))
    gpoints: defaultdict[str, defaultdict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

    for acc, fills in zip(accs, fills_list):
        for fill in fills:
            if not fill["coin"].startswith("hyna:"):
                continue
            dt = datetime.fromtimestamp(fill["time"] / 1000, tz=timezone.utc)
            gtrades[to_week(dt)][acc.name].append(fill)

    for acc, rewards in zip(accs, rewards_list):
        for h in rewards.history:
            n = int(h.id.removeprefix("reward-week-"))
            wk = to_week(GENESIS + timedelta(weeks=n - 1))
            gpoints[wk][acc.name] += h.enaxPoints

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

    render_stats(periods_data, periods_to_show)


# MARK: Main


async def main():
    cli = await create_cli("hyena", "configs/hyena.toml", ["privkey"])
    cfg = StrategyConfig.load(cli.config)

    accs = [(HyenaClient.from_config(x), x.enabled) for x in cfg.accounts]
    all_accs, act_accs = [c for c, _ in accs], [c for c, e in accs if e]

    match cli.command:
        case "info":
            await print_info(all_accs)
        case "positions":
            await print_positions(act_accs)
        case "stats":
            await print_stats(all_accs, period=cli.group, filter_period=cli.filter)
        case "close":
            await close_all(act_accs)
        case "trade":
            await run_groups(cfg, act_accs)


if __name__ == "__main__":
    run_app(main())
