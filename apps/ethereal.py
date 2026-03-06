# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Crafted with love and ctrl+c
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from functools import partial

from pydantic import BaseModel, Field, SecretStr, field_validator

from clients.ethereal import EtherealClient, EtherealPoint, EtherealPosition
from lib.cli import create_cli, run_app
from lib.crypto import decrypt_value, is_encrypted
from lib.store import DataStore
from lib.table import AutoTable, Column
from lib.utils import gather_accs, parse_filter, short_addr, to_period_day, to_period_week
from strategy.delta import run_groups
from strategy.models import StrategyConfig, load_config
from strategy.trading import close_all

GENESIS = datetime(2025, 12, 18, tzinfo=timezone.utc)


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


async def sync_trades(acc: EtherealClient, ttl: int) -> list[EtherealPosition]:
    store_path = f".cache/ethereal_{short_addr(acc.address)}_trades.pkl"
    store = DataStore(store_path, id_key="id", model=EtherealPosition)
    await store.sync(lambda _: acc.raw_positions(open_only=False), ttl_sec=ttl)
    return store.get_all()


async def sync_points(acc: EtherealClient, ttl: int) -> list[EtherealPoint]:
    store_path = f".cache/ethereal_{short_addr(acc.address)}_points.pkl"
    store = DataStore(store_path, id_key="id", model=EtherealPoint)
    await store.sync(lambda _: acc.points(), ttl_sec=ttl)
    return store.get_all()


# MARK: Reports


async def print_info(accs: list[EtherealClient]):
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

    async def row(acc: EtherealClient):
        await acc.warmup()
        p = await acc.profile() if await acc.registered() else None
        a = short_addr(acc.address)
        if not p:
            return ("✗", acc.name, a, 0, 0, 0, 0)
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance)

    for r in await gather_accs(accs, row):
        tbl.add_row(*r)

    tbl.print()


async def print_stats(accs: list[EtherealClient], period="week", filter_period="all", force=False):
    gpos: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    gpts: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    ttl = 0 if force else 3600

    period_fn = to_period_day if period == "day" else partial(to_period_week, genesis=GENESIS)

    all_trades, all_points = await asyncio.gather(
        gather_accs(accs, lambda acc: sync_trades(acc, ttl)),
        gather_accs(accs, lambda acc: sync_points(acc, ttl)),
    )
    for acc, trades in zip(accs, all_trades):
        for t in trades:
            gpos[period_fn(t.created_at)][acc.name].append(t)
    for acc, pts in zip(accs, all_points):
        for p in pts:
            gpts[period_fn(p.started_at)][acc.name] += p.total_points

    tbl = AutoTable(
        Column("Account", justify="left"),
        Column("Trades", "{:,}", total=sum),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.0f}", total=sum),
        Column("P/Price", "{:,.4f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("V/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Volume"] * Decimal(1e5)),
        Column("Fees", "{:,.2f}", total=sum),
        Column("Fee, %", "{:.3%}", compute=lambda r: r["Fees"] / r["Volume"]),
        Column("Total Vol", "{:,.0f}", total=sum, grand_total=False),
    )

    all_periods = sorted(gpos.keys() | gpts.keys())
    periods_to_show = parse_filter(filter_period, all_periods)

    all_names = [x.name for x in accs]
    tvol = defaultdict(Decimal)

    for p in periods_to_show:
        tbl.subgroup(f"{p}")
        acc_names = sorted(gpos[p].keys() | gpts[p].keys())
        acc_names = [x for x in all_names if x in acc_names]  # keep order of accounts
        for acc_name in acc_names:
            positions = gpos[p][acc_name]
            pts = gpts.get(p, {}).get(acc_name, Decimal(0))
            if not positions and pts < 1:
                continue

            vol = sum(pos.total_inc + pos.total_dec for pos in positions)
            fee = sum(pos.fees_usd for pos in positions)
            fnd = sum(pos.funding_usd for pos in positions)
            pnl = sum(pos.realized_pnl for pos in positions) - fee - fnd
            tvol[acc_name] += vol
            tbl.add_row(acc_name, len(positions), vol, -pnl, pts, fee, tvol[acc_name])

    tbl.print()


# MARK: Main


def client_from_config(cfg: AccountConfig) -> EtherealClient:
    return EtherealClient(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)


async def main():
    cli = create_cli("ethereal", "configs/ethereal.toml", ["privkey"])
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
