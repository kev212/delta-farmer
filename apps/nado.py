# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Refactoring is just future procrastination
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import TypeVar

from pydantic import BaseModel, Field, SecretStr, field_validator

from clients.nado import NadoClient, NadoPoint, NadoTrade
from lib.cli import create_cli, run_app
from lib.crypto import decrypt_value, is_encrypted
from lib.store import DataStore
from lib.table import AutoTable, Column
from lib.utils import gather_accs, parse_filter, short_addr
from strategy.delta import run_groups
from strategy.models import StrategyConfig, load_config
from strategy.trading import close_all

T = TypeVar("T")
DD = defaultdict[str, defaultdict[str, T]]

_OFF_START = datetime(2026, 1, 16, tzinfo=timezone.utc)  # Off Season start
_SEASON_START = datetime(2026, 1, 31, tzinfo=timezone.utc)  # Week 1 start


def week_name(dt: datetime) -> str:
    """Returns period label for a given UTC datetime: ALP / OFF / W01 / W02 / ..."""
    if dt < _OFF_START:
        return "ALP"
    if dt < _SEASON_START:
        return "OFF"
    return f"W{((dt - _SEASON_START).days + 1) // 7 + 1:02d}"


class AccountConfig(BaseModel):
    name: str
    privkey: SecretStr = Field(repr=False)
    proxy: str | None = None
    enabled: bool = True

    @field_validator("privkey", mode="before")
    @classmethod
    def decrypt_secret(cls, v: str) -> str:
        return decrypt_value(v) if isinstance(v, str) and is_encrypted(v) else v


class Config(StrategyConfig):
    accounts: list[AccountConfig]

    @classmethod
    def load(cls, filepath: str):
        return load_config(cls, filepath)


# MARK: Storages


async def sync_trades(acc: NadoClient, ttl: int) -> list[NadoTrade]:
    store_path = f".cache/nado_{short_addr(acc.address)}_trades.pkl"
    store = DataStore(store_path, id_key="digest", model=NadoTrade)
    await store.sync(lambda since: acc.trades(since), ttl_sec=ttl)
    return store.get_all()


async def sync_points(acc: NadoClient, ttl: int) -> list[NadoPoint]:
    store_path = f".cache/nado_{short_addr(acc.address)}_points.pkl"
    store = DataStore(store_path, id_key="since", model=NadoPoint)
    await store.sync(lambda _: acc.points(), ttl_sec=ttl)
    return store.get_all()


# MARK: Reports


async def print_info(accs: list[NadoClient]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.2f}", total=sum),
        Column("P/Price", "{:,.4f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
    )

    async def row(acc: NadoClient):
        await acc.warmup()
        p = await acc.profile() if await acc.registered() else None
        a = short_addr(acc.address)
        if not p:
            return ("✗", acc.name, a, 0, 0, 0, 0)
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance)

    for r in await asyncio.gather(*[row(acc) for acc in accs]):
        tbl.add_row(*r)

    tbl.print()


async def print_stats(accs: list[NadoClient], period="week", filter_period="all", force=False):
    gtrades: DD[list[NadoTrade]] = defaultdict(lambda: defaultdict(list))
    gpoints: DD[Decimal] = defaultdict(lambda: defaultdict(Decimal))
    ttl = 0 if force else 3600

    def period_fn(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d") if period == "day" else week_name(dt)

    all_trades, all_points = await asyncio.gather(
        gather_accs(accs, lambda acc: sync_trades(acc, ttl)),
        gather_accs(accs, lambda acc: sync_points(acc, ttl)),
    )
    for acc, trades in zip(accs, all_trades):
        for t in trades:
            gtrades[period_fn(t.created_at)][acc.name].append(t)
    for acc, pts in zip(accs, all_points):
        for p in pts:
            gpoints[period_fn(p.since)][acc.name] = p.points

    all_periods = sorted(gtrades.keys() | gpoints.keys())
    periods_to_show = parse_filter(filter_period, all_periods)

    tbl = AutoTable(
        Column("Account", justify="left"),
        Column("Trades", "{:,}", total=sum),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.2f}", total=sum),
        Column("P/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("V/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Volume"] * Decimal(1e5)),
        Column("Fees", "{:,.2f}", total=sum),
        Column("Fee, %", "{:.3%}", compute=lambda r: r["Fees"] / r["Volume"]),
        Column("Total Vol", "{:,.0f}", total=sum, grand_total=False),
    )

    all_names = [x.name for x in accs]
    tvol = defaultdict(Decimal)

    for pk in periods_to_show:
        tbl.subgroup(pk)
        acc_names = sorted(gtrades[pk].keys() | gpoints[pk].keys())
        acc_names = [x for x in all_names if x in acc_names]  # keep order of accounts
        for acc_name in acc_names:
            trades = gtrades[pk].get(acc_name, [])
            points = gpoints[pk].get(acc_name, Decimal(0))
            vol = sum(t.amount * t.price for t in trades)
            pnl = sum(t.realized_pnl - t.fee for t in trades)
            fee = sum(t.fee for t in trades)
            tvol[acc_name] += vol
            tbl.add_row(acc_name, len(trades), vol, -pnl, points, fee, tvol[acc_name])

    tbl.print()


# MARK: Main


def client_from_config(cfg: AccountConfig) -> NadoClient:
    return NadoClient(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)


async def main():
    cli = create_cli("nado", "configs/nado.toml", ["privkey"])
    cfg = Config.load(cli.config)

    accs = [(client_from_config(x), x.enabled) for x in cfg.accounts]
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


if __name__ == "__main__":
    run_app(main())
