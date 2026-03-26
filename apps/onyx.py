# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI

from clients.onyx import OnyxClient
from lib.cli import create_cli, run_app
from lib.table import AutoTable, Column
from lib.utils import gather_accs, short_addr
from strategy import StrategyConfig
from strategy.runner import close_all, print_positions, run_groups

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
            print(
                "Stats are not available for Onyx — use `info` to see Onyx volume and trade count."
            )
        case "trade":
            await run_groups(cfg, act_accs)


if __name__ == "__main__":
    run_app(main())
