# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from clients.zero1 import ZeroOneClient
from lib.cli import create_cli, run_app
from lib.logger import logger
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import gather_accs, parse_filter, short_addr
from strategy import StrategyConfig
from strategy.runner import close_all, print_positions, run_groups

# MARK: Reports


async def print_info(accs: list[ZeroOneClient]):
    print("!! 01.xyz is in beta — not all data is available yet\n", file=sys.stderr)
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Balance", "{:,.2f}", total=sum),
        Column("Volume", "{:,.0f}", total=sum),
        Column("PnL", "{:,.2f}", total=sum),
    )

    async def row(acc: ZeroOneClient):
        try:
            p = await acc.profile()
            return ("✓", acc.name, short_addr(acc.address), p.balance, p.volume, p.pnl)
        except Exception as e:
            logger.warning(f"[{acc.name}] profile failed: {e}")
            return ("✗", acc.name, short_addr(acc.address), Decimal(0), Decimal(0), Decimal(0))

    for r in await gather_accs(accs, row):
        tbl.add_row(*r)

    tbl.print()


async def _fetch_paged(acc: ZeroOneClient, path: str, **params) -> list[dict]:
    items, cursor = [], None
    while True:
        p = {**params, "pageSize": 255}
        if cursor:
            p["startExclusive"] = cursor
        rep = await acc.http.request("GET", path, params=p)
        if not rep.ok:
            break
        data = rep.json()
        batch = data.get("items", [])
        items += batch
        cursor = data.get("nextStartInclusive")
        if not cursor or len(batch) < 255:
            break
    return items


async def _fetch_stats(acc: ZeroOneClient) -> dict:
    _, acc_id = await acc._ensure_session()
    maker, taker, pnl = await asyncio.gather(
        _fetch_paged(acc, "/trades", makerId=acc_id),
        _fetch_paged(acc, "/trades", takerId=acc_id),
        _fetch_paged(acc, f"/account/{acc_id}/history/pnl"),
    )
    tr = await acc.http.request("GET", f"/market/0/fees/taker/{acc_id}")
    mr = await acc.http.request("GET", f"/market/0/fees/maker/{acc_id}")
    return {
        "maker": maker,
        "taker": taker,
        "pnl": pnl,
        "taker_rate": Decimal(str(tr.json())) if tr.ok else Decimal("0.00035"),
        "maker_rate": Decimal(str(mr.json())) if mr.ok else Decimal("0.0001"),
    }


async def print_stats(accs: list[ZeroOneClient], filter_period: str = "all"):
    print("!! 01.xyz is in beta — not all data is available yet\n", file=sys.stderr)
    stats_list = await gather_accs(accs, _fetch_stats)

    gpnl: defaultdict[str, defaultdict[str, list]] = defaultdict(lambda: defaultdict(list))
    gtrades: defaultdict[str, defaultdict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"maker": [], "taker": []})
    )

    for acc, stats in zip(accs, stats_list):
        for t in stats["pnl"]:
            dt = datetime.fromisoformat(t["time"].rstrip("Z")).replace(tzinfo=timezone.utc)
            gpnl[dt.strftime("%Y-W%V")][acc.name].append(Decimal(str(t["tradingPnl"])))
        for role in ("maker", "taker"):
            for t in stats[role]:
                dt = datetime.fromisoformat(t["time"].rstrip("Z")).replace(tzinfo=timezone.utc)
                gtrades[dt.strftime("%Y-W%V")][acc.name][role].append(t)

    all_periods = sorted(gtrades.keys() | gpnl.keys())
    periods_to_show = parse_filter(filter_period, all_periods)
    all_names = [acc.name for acc in accs]
    rates = {acc.name: (s["taker_rate"], s["maker_rate"]) for acc, s in zip(accs, stats_list)}

    periods_data: dict[str, list[PeriodRow]] = {}
    for pk in all_periods:
        rows = []
        for name in all_names:
            pnl_vals = gpnl[pk].get(name, [])
            trade_data = gtrades[pk].get(name, {"maker": [], "taker": []})
            if not pnl_vals and not trade_data["maker"] and not trade_data["taker"]:
                continue
            taker_rate, maker_rate = rates[name]

            def _vol(ts: list[dict]) -> Decimal:
                return sum(
                    (Decimal(str(t["price"])) * Decimal(str(t["baseSize"])) for t in ts), Decimal(0)
                )

            taker_vol = _vol(trade_data["taker"])
            maker_vol = _vol(trade_data["maker"])
            vol = taker_vol + maker_vol
            fee = taker_vol * taker_rate + maker_vol * maker_rate
            burn = -sum(pnl_vals, Decimal(0))
            count = len(trade_data["maker"]) + len(trade_data["taker"])
            rows.append(PeriodRow(name, count, vol, burn, Decimal(0), fee))
        periods_data[pk] = rows

    render_stats(periods_data, periods_to_show)


# MARK: Main


async def main():
    cli = await create_cli("zero1", "configs/zero1.toml", ["privkey"])
    cfg = StrategyConfig.load(cli.config)

    accs = [(ZeroOneClient.from_config(x), x.enabled) for x in cfg.accounts]
    all_accs, act_accs = [c for c, _ in accs], [c for c, e in accs if e]

    match cli.command:
        case "info":
            await print_info(all_accs)
        case "positions":
            await print_positions(act_accs)
        case "stats":
            await print_stats(all_accs, filter_period=cli.filter)
        case "close":
            await close_all(act_accs)
        case "trade":
            await run_groups(cfg, act_accs)


if __name__ == "__main__":
    run_app(main())
