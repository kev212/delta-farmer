# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Crafted with love and ctrl+c
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from functools import partial

from pydantic import BaseModel, Field, SecretStr, field_validator

from clients.ethereal import Client
from strategy.models import StrategyConfig, load_config
from strategy.strategy import DeltaStrategy
from strategy.trading import close_all
from utils.cli import create_cli, run_app
from utils.crypto import decrypt_value, is_encrypted
from utils.helpers import parse_filter, short_addr, to_period_day, to_period_week
from utils.table import AutoTable, Column

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


def client_from_config(cfg: AccountConfig) -> Client:
    return Client(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)


def load_accs(cfg: Config) -> list[Client]:
    return [client_from_config(x) for x in cfg.accounts]


def load_trading_clients(cfg: Config) -> list[Client]:
    return [client_from_config(x) for x in cfg.accounts if x.enabled]


async def print_info(accs: list[Client]):
    tbl = AutoTable(
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.0f}", total=sum),
        Column("P/Price", "{:,.4f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("Balance", "{:,.2f}", total=sum),
    )

    for acc in accs:
        bal, vol, fees, pts = await asyncio.gather(
            acc.balance(), acc.total_volume(), acc.total_fees(), acc.points()
        )
        tbl.add_row(acc.name, short_addr(acc.address), vol, fees, pts.total_points, bal)

    tbl.print()


async def print_stats(accs: list[Client], period="week", filter_period="all", force=False):
    gpos: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    gpts: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

    period_fn = to_period_day if period == "day" else partial(to_period_week, genesis=GENESIS)

    for acc in accs:
        positions = await acc.raw_positions(open_only=False, limit=2000)
        points_history = await acc.points_history()

        # Group positions by period
        for pos in positions:
            p = period_fn(pos.created_at)
            gpos[p][acc.name].append(pos)

        # Group points by period
        for pt in points_history:
            p = period_fn(pt.started_at)
            gpts[p][acc.name] += pt.points

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

    tvol: dict[str, Decimal] = defaultdict(Decimal)
    for p in periods_to_show:
        tbl.subgroup(f"{p}")
        acc_names = sorted(gpos[p].keys() | gpts[p].keys())
        for acc_name in acc_names:
            positions = gpos[p][acc_name]
            points = gpts.get(p, {}).get(acc_name, Decimal(0))

            vol = sum(pos.total_inc + pos.total_dec for pos in positions)
            pnl = sum(pos.realized_pnl for pos in positions)
            fee = sum(pos.fees_usd + pos.funding_usd for pos in positions)
            tvol[acc_name] += vol
            tbl.add_row(acc_name, len(positions), vol, -pnl, points, fee, tvol[acc_name])

    tbl.print()


async def main():
    cli = create_cli("ethereal", "configs/ethereal.toml", ["privkey"])
    cfg = Config.load(cli.config)
    accs = load_accs(cfg)

    match cli.command:
        case "info":
            await print_info(accs)
        case "stats":
            await print_stats(accs, period=cli.group, filter_period=cli.filter, force=cli.sync)
        case "close":
            await close_all(load_trading_clients(cfg))
        case "trade":
            await DeltaStrategy(cfg, load_trading_clients(cfg)).run()


if __name__ == "__main__":
    run_app(main())
