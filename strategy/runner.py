# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Small plans, fewer surprises
import asyncio
import random
from collections.abc import Sequence
from itertools import batched

from lib import telegram as tg
from lib import telemetry, utils
from lib.http import FatalError
from lib.logger import logger

from .delta import DeltaStrategy
from .models import StrategyConfig, TradingClient


async def close_all(clients: Sequence[TradingClient]) -> None:
    """Warmup, cancel all orders, and close all positions. Used by the CLI close command."""
    for client in clients:
        await client.warmup()
        count1 = await client.cancel_all_orders()
        count2 = await client.close_all_positions()
        logger.info(f"{client.name}: Canceled {count1} orders, closed {count2} positions")


# MARK: Groups


async def _warmup_all(accs: Sequence[TradingClient]) -> None:
    rs = await asyncio.gather(*[a.warmup() for a in accs], return_exceptions=True)
    failed = [a.name for a, r in zip(accs, rs) if isinstance(r, Exception)]
    if failed:
        raise FatalError(f"Warmup failed: {', '.join(failed)}")

    rs = await asyncio.gather(*[a.registered() for a in accs], return_exceptions=True)
    failed = [a.name for a, r in zip(accs, rs) if isinstance(r, Exception) or r is False]
    if failed:
        raise FatalError(f"Not registered: {', '.join(failed)}")


async def _run_group(
    cfg: StrategyConfig,
    name: str,
    accs: Sequence[TradingClient],
    stop_event: asyncio.Event,
    stagger: float = 0,
) -> None:
    strategy = DeltaStrategy(cfg, accs, stop_event=stop_event)
    with logger.contextualize(group=name):
        await asyncio.sleep(stagger) if stagger else None
        logger.info(f"Starting group with accounts: {', '.join(a.name for a in accs)}")
        await strategy.run()  # all exceptions should be handled inside run()


def _check_cfg(cfg: StrategyConfig, accs: Sequence[TradingClient]):
    n = len(accs)

    if cfg.trade_size_usd is None and cfg.trade_size_pct is None:
        raise FatalError("either trade_size_usd or trade_size_pct must be specified in config")
    if cfg.trade_size_usd is not None and cfg.trade_size_pct is not None:
        raise FatalError("trade_size_usd and trade_size_pct are mutually exclusive")

    if n < 2:
        raise FatalError(f"At least 2 accounts are required for trading, got {n}")

    if cfg.symbols_per_trade > 1 and len(cfg.symbols) != cfg.symbols_per_trade:
        raise FatalError(
            f"symbols_per_trade={cfg.symbols_per_trade} requires exactly "
            f"{cfg.symbols_per_trade} symbols, got {len(cfg.symbols)}"
        )

    if cfg.group_size is None and n > 5:
        raise FatalError("Single-group mode supports up to 5 enabled accounts")

    if cfg.group_size is not None and n % cfg.group_size != 0:
        raise FatalError(f"{n} enabled accounts is not divisible by group_size={cfg.group_size}")

    if cfg.group_size is not None and cfg.first_as_prime:
        cfg.first_as_prime = False
        logger.warning("group_size is set, ignoring first_as_prime=true")

    return cfg, accs


async def _balance_sorted(accs: Sequence[TradingClient]) -> list[TradingClient]:
    rs = await asyncio.gather(*[a.balance() for a in accs])
    pairs = [(acc, bal) for acc, bal in zip(accs, rs)]
    pairs.sort(key=lambda x: x[1])
    return [acc for acc, _ in pairs]


async def run_groups(cfg: StrategyConfig, accs: Sequence[TradingClient]) -> None:
    cfg, accs = _check_cfg(cfg, accs)
    await _warmup_all(accs)

    tg.start()
    telemetry.track(
        "trade_started",
        {
            "account_count": len(accs),
            "use_limit": cfg.use_limit,
            "group_mode": cfg.group_size is not None,
            "regroup_interval": cfg.regroup_interval is not None,
            "first_as_prime": cfg.first_as_prime,
            "telegram_enabled": tg.enabled(),
        },
    )

    if not cfg.group_size:  # Single group mode, no regrouping
        return await DeltaStrategy(cfg, accs).run()

    while True:
        print("-" * 60)
        # sort by balance only if regrouping requested - otherwise keep config order
        accs = await _balance_sorted(accs) if cfg.regroup_interval else accs
        grps = [list(g) for g in batched(accs, cfg.group_size)]
        logger.info(f"Running trading with {len(grps)} groups ({len(accs)} accounts)")

        tasks: list[asyncio.Task] = []
        stop_event = asyncio.Event()

        for i, grp_accounts in enumerate(grps):
            stagger = i * random.uniform(10, 30)
            name = f"{i + 1:02d}"
            coro = _run_group(cfg, name, grp_accounts, stop_event, stagger)
            tasks.append(asyncio.create_task(coro, name=f"delta-{name}"))

        if cfg.regroup_interval is None:  # if not regrouping, just run until manually stopped
            await asyncio.gather(*tasks, return_exceptions=True)
            return

        await asyncio.sleep(cfg.regroup_interval)
        stop_event.set()

        max_wait = int(cfg.limit_wait) + int(cfg.trade_duration.max) + 60
        await utils.gather_cancel(tasks, max_wait)
