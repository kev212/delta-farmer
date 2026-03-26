# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | No AI was harmed making this
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from functools import partial

from clients.omni import OmniClient, OmniPoint
from lib.cli import create_cli, run_app
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import gather_accs, parse_filter, short_addr, to_period_day, to_period_week
from strategy import StrategyConfig
from strategy.runner import close_all, print_positions, run_groups

# https://docs.variational.io/omni/rewards/points
# https://omni.variational.io/points (UI counts from -1 week)
GENESIS = datetime(2025, 12, 17 - 6, tzinfo=timezone.utc)

to_week_name = partial(to_period_week, genesis=GENESIS)


# MARK: Storages


async def sync_raw(acc: OmniClient, endpoint: str, ttl: int) -> list[dict]:
    store_name = endpoint.strip("/").replace("/", "_")
    store_path = f".cache/omni_{short_addr(acc.address)}_{store_name}.pkl"
    store = DataStore(store_path, id_key="id")
    await store.sync(lambda since: acc.fetch_history(endpoint, since=since), ttl)
    return store.get_all()


async def sync_points(acc: OmniClient, ttl: int) -> list[OmniPoint]:
    store_path = f".cache/omni_{short_addr(acc.address)}_points.pkl"
    store = DataStore(store_path, id_key="start_window", model=OmniPoint)
    await store.sync(lambda _: acc.points(), ttl_sec=ttl)
    return store.get_all()


# MARK: Reports


async def print_info(accs: list[OmniClient]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.1f}", total=sum),
        Column("P/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
        Column("Rank", justify="right"),
        Column("Ref", justify="left"),
    )

    async def row(acc: OmniClient):
        await acc.warmup()
        p = await acc.profile() if await acc.registered() else None
        a = short_addr(acc.address)
        if not p:
            return ("✗", acc.name, a, "", 0, 0, 0, "", "")
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance, p.rank, p.ref_code)

    for r in await gather_accs(accs, row):
        tbl.add_row(*r)

    tbl.print()


async def print_stats(accs: list[OmniClient], period="week", filter_period="all", force=False):
    gcnt = defaultdict(lambda: defaultdict(int))
    gpnl = defaultdict(lambda: defaultdict(Decimal))
    gvol = defaultdict(lambda: defaultdict(Decimal))
    gpts = defaultdict(lambda: defaultdict(Decimal))

    period_fn = to_period_day if period == "day" else to_week_name
    ttl = 0 if force else 3600

    all_transfers, all_trades, all_points = await asyncio.gather(
        gather_accs(accs, lambda acc: sync_raw(acc, "/transfers", ttl)),
        gather_accs(accs, lambda acc: sync_raw(acc, "/trades", ttl)),
        gather_accs(accs, lambda acc: sync_points(acc, ttl)),
    )
    for acc, transfers, trades, points in zip(accs, all_transfers, all_trades, all_points):
        transfers = [t for t in transfers if t["status"] == "confirmed"]
        transfers = [t for t in transfers if t["transfer_type"] in ("funding", "realized_pnl")]
        trades = [t for t in trades if t["status"] == "confirmed"]

        for p in points:
            week = period_fn(p.start_window)
            gpts[week][acc.name] = p.total_points

        for t in transfers:
            p = period_fn(datetime.fromisoformat(t["created_at"]))
            gpnl[p][acc.name] += Decimal(t["qty"])

        for t in trades:
            p = period_fn(datetime.fromisoformat(t["created_at"]))
            usd_value = Decimal(t["price"]) * Decimal(t["qty"])
            gvol[p][acc.name] += usd_value
            gcnt[p][acc.name] += 1

    all_periods = sorted(gpnl.keys() | gvol.keys() | gpts.keys())
    periods_to_show = parse_filter(filter_period, all_periods)
    all_names = [x.name for x in accs]

    periods_data: dict[str, list[PeriodRow]] = {}
    for p in all_periods:
        acc_names = [
            n for n in all_names if n in (gpnl[p].keys() | gvol[p].keys() | gpts[p].keys())
        ]
        rows = []
        for acc_name in acc_names:
            cnt = gcnt[p][acc_name] or 0
            pnl = gpnl[p][acc_name] or Decimal(0)
            vol = gvol[p][acc_name] or Decimal(0)
            pts = gpts[p][acc_name] or Decimal(0)
            rows.append(PeriodRow(acc_name, cnt, vol, -pnl, pts, Decimal(0)))
        periods_data[p] = rows

    render_stats(periods_data, periods_to_show, fees=False, points_fmt="{:,.2f}")


# MARK: Main


async def main():
    cli = await create_cli("omni", "configs/omni.toml", ["privkey"])
    cfg = StrategyConfig.load(cli.config)

    accs = [(OmniClient.from_config(x), x.enabled) for x in cfg.accounts]
    all_accs, act_accs = [c for c, _ in accs], [c for c, e in accs if e]

    match cli.command:
        case "info":
            await print_info(all_accs)
        case "stats":
            await print_stats(all_accs, period=cli.group, filter_period=cli.filter, force=cli.force)
        case "close":
            await close_all(act_accs)
        case "trade":
            await run_groups(cfg, act_accs)
        case "positions":
            await print_positions(act_accs)


if __name__ == "__main__":
    run_app(main())
