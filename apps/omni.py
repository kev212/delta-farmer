# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | No AI was harmed making this
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from functools import partial

from pydantic import BaseModel, Field, SecretStr, field_validator

from clients.omni import OmniClient, OmniPoint
from lib.cli import create_cli, run_app
from lib.crypto import decrypt_value, is_encrypted
from lib.store import DataStore
from lib.table import AutoTable, Column
from lib.utils import gather_accs, parse_filter, short_addr, to_period_day, to_period_week
from strategy.delta import run_groups
from strategy.models import StrategyConfig, load_config
from strategy.trading import close_all

# https://docs.variational.io/omni/rewards/points
# https://omni.variational.io/points (UI counts from -1 week)
GENESIS = datetime(2025, 12, 17 - 6, tzinfo=timezone.utc)

to_week_name = partial(to_period_week, genesis=GENESIS)


class AccountConfig(BaseModel):
    name: str
    privkey: SecretStr = Field(repr=False)
    proxy: str | None = None
    enabled: bool = True

    @field_validator("privkey", mode="before")
    @classmethod
    def decrypt_privkey(cls, v: str) -> str:
        return decrypt_value(v) if isinstance(v, str) and is_encrypted(v) else v


class Config(StrategyConfig):
    accounts: list[AccountConfig]

    @classmethod
    def load(cls, filepath: str):
        return load_config(cls, filepath)


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
    )

    async def row(acc: OmniClient):
        await acc.warmup()
        p = await acc.profile() if await acc.registered() else None
        a = short_addr(acc.address)
        if not p:
            return ("✗", acc.name, a, 0, 0, 0, 0)
        return ("✓", acc.name, a, p.volume, -p.pnl, p.points, p.balance)

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

    tbl = AutoTable(
        Column("Account", justify="left"),
        Column("Trades", "{:,}", total=sum),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.2f}", total=sum),
        Column("P/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column("V/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Volume"] * Decimal(1e5)),
        Column("Total Vol", "{:,.0f}", total=sum, grand_total=False),
    )

    all_periods = sorted(gpnl.keys() | gvol.keys() | gpts.keys())
    periods_to_show = parse_filter(filter_period, all_periods)

    all_names = [x.name for x in accs]
    tvol = defaultdict(Decimal)

    for p in periods_to_show:
        tbl.subgroup(f"{p}")
        acc_names = sorted(gpnl[p].keys() | gvol[p].keys() | gpts[p].keys())
        acc_names = [x for x in all_names if x in acc_names]  # keep order of accounts
        for acc_name in acc_names:
            cnt = gcnt[p][acc_name] or 0
            pnl = gpnl[p][acc_name] or 0
            vol = gvol[p][acc_name] or 0
            pts = gpts[p][acc_name] or 0
            tvol[acc_name] += vol
            tbl.add_row(acc_name, cnt, vol, -pnl, pts, tvol[acc_name])

    tbl.print()


# MARK: Main


def client_from_config(cfg: AccountConfig) -> OmniClient:
    return OmniClient(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)


async def main():
    cli = create_cli("omni", "configs/omni.toml", ["privkey"])
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
