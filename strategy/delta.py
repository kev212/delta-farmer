# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Code so clean it squeaks
import asyncio
import random
from decimal import Decimal
from itertools import batched
from typing import Sequence

from lib import telemetry, utils
from lib.http import FatalError
from lib.logger import logger
from strategy.execution import (
    TradeAction,
    check_min_trade_sizes,
    close_all,
    close_positions,
    ensure_leverage,
    open_positions,
    wait_with_checks,
)
from strategy.models import StrategyConfig
from strategy.trading import Side, TradingClient, opposite_side, usd_to_qty


class DeltaStrategy:
    """
    Delta-neutral strategy that works with any TradingClient.
    Opens opposite positions on multiple accounts.
    """

    def __init__(
        self,
        cfg: StrategyConfig,
        accounts: Sequence[TradingClient],
        stop_event: asyncio.Event | None = None,
    ):
        self.cfg = cfg
        self.accounts = list(accounts)
        self.stop_event = stop_event
        self.initial_bal = Decimal(0)

    # MARK: Core trading flow

    async def _loop(self):
        await close_all(self.accounts)  # clean up leftovers from a previous run

        # cycles until an exception; run() catches, waits, and retries
        while True:
            try:
                # print sep for each trade cycle in single-group mode
                print("-" * 60) if not self.cfg.group_size else None

                await self.trade_cycle()
                utils.raise_if_cancelled(self.stop_event)

                wait_sec = self.cfg.trade_cooldown.sample()
                logger.info(utils.wait_msg(wait_sec))
                await utils.interruptible_sleep(wait_sec, self.stop_event)
            except asyncio.CancelledError:
                # CancelledError is BaseException (not Exception) since py3.8 — catch explicitly
                await close_all(self.accounts)
                raise
            except Exception as e:
                logger.warning(f"Trade cycle failed {type(e)}: {e}")
                await close_all(self.accounts)  # best-effort cleanup, may also fail
                raise e

    async def run(self):
        balance_inited = False

        while True:  # restart _loop() after transient failures; exit only on cancel/fatal
            try:
                if not balance_inited:
                    bals = await self.get_balances()
                    self.initial_bal = sum(bal for _, bal in bals)
                    balance_inited = True

                await self._loop()
            except asyncio.CancelledError:
                return  # graceful shutdown
            except Exception as e:
                wait_sec = 60 * 3
                logger.error(f"Trade failed with {type(e)}: {e} - {utils.wait_msg(wait_sec)}")
                await asyncio.sleep(wait_sec)

    async def trade_cycle(self):
        """One complete trade cycle."""
        # 1. Get balances, find safe pairs
        balances = await self.get_balances()
        bal_str = " | ".join([f"{name} {bal:.2f}" for name, bal in balances])
        bal_str = f"{sum(bal for _, bal in balances):.2f} = " + bal_str
        logger.info(f"Balances: {bal_str}")

        actions = self.plan_trades(balances)
        if actions is None:
            logger.error("No valid account combination found for trading.")
            return

        # 2. Calculate quantities
        market = random.choice(self.cfg.markets)
        price = await actions[0].client.get_price(market)
        lot_size = await actions[0].client.get_lot_size(market)

        for act in actions:
            act.qty = usd_to_qty(act.size_usd, price, lot_size)

        size_usd = sum(x.size_usd for x in actions)
        rest_sizes = " ".join([str(x.size_usd) for x in actions[1:]])
        rest_sizes = f"{sum(x.size_usd for x in actions[1:])} ({rest_sizes})"
        logger.info(f"Trade {market}: {size_usd} = {actions[0].size_usd} + {rest_sizes}")

        # 3. Check min trade size per account
        await check_min_trade_sizes(actions, market)

        # 4. Set leverage
        await ensure_leverage(self.accounts, market, self.cfg.leverage)

        # 5. Open positions
        await open_positions(actions, market, self.cfg)

        # 6. Wait with safety checks
        success = await wait_with_checks(actions, market, self.cfg, self.stop_event)

        # 7. Close positions
        await close_positions(
            actions, market, self.accounts, self.cfg, use_limit=success and self.cfg.use_limit
        )

        # 8. Report P/L
        await self.report_pnl(balances)

    # MARK: Helpers

    async def get_balances(self) -> list[tuple[str, float]]:
        bals = await asyncio.gather(*[acc.balance() for acc in self.accounts])
        return [(acc.name, float(b)) for acc, b in zip(self.accounts, bals)]

    def plan_trades(self, balances: list[tuple[str, float]]) -> list[TradeAction] | None:
        if self.cfg.first_as_main:
            balances = balances[:1] + utils.shuffle(balances[1:])
        else:
            balances = utils.shuffle(balances)

        size_usd = self.cfg.trade_size_usd.sample()
        pairs = utils.find_safe_pair(balances, size_usd, self.cfg.leverage)
        if pairs is None:
            return None

        accounts_map = {acc.name: acc for acc in self.accounts}
        main_side: Side = random.choice(["bid", "ask"])

        return [
            TradeAction(
                client=accounts_map[name],
                side=main_side if i == 0 else opposite_side(main_side),
                size_usd=Decimal(str(size)),
            )
            for i, (name, size) in enumerate(pairs)
        ]

    async def report_pnl(self, was: list[tuple[str, float]]):
        now = await self.get_balances()
        diff_sum = sum(x[1] for x in now) - sum(x[1] for x in was)
        diff_str = [(x[0], x[1] - y[1]) for x, y in zip(now, was)]
        diff_str = " | ".join([f"{name} {diff:+.2f}" for name, diff in diff_str])
        total_pnl = sum(x[1] for x in now) - float(self.initial_bal)
        logger.info(f"Δ {diff_sum:+.2f} ~ {diff_str}; Total P/L: {total_pnl:+.2f}")


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

    if n < 2:
        raise FatalError(f"At least 2 accounts are required for trading, got {n}")

    if cfg.group_size is None and n > 5:
        raise FatalError("Single-group mode supports up to 5 enabled accounts")

    if cfg.group_size is not None and n % cfg.group_size != 0:
        raise FatalError(f"{n} enabled accounts is not divisible by group_size={cfg.group_size}")

    if cfg.group_size is not None and cfg.first_as_main:
        cfg.first_as_main = False
        logger.warning("group_size is set, ignoring first_as_main=true")

    return cfg, accs


async def _balance_sorted(accs: Sequence[TradingClient]) -> list[TradingClient]:
    rs = await asyncio.gather(*[a.balance() for a in accs])
    pairs = [(acc, bal) for acc, bal in zip(accs, rs)]
    pairs.sort(key=lambda x: x[1])
    return [acc for acc, _ in pairs]


async def run_groups(cfg: StrategyConfig, accs: Sequence[TradingClient]) -> None:
    cfg, accs = _check_cfg(cfg, accs)
    await _warmup_all(accs)

    telemetry.track(
        "trade_started",
        {
            "account_count": len(accs),
            "use_limit": cfg.use_limit,
            "group_mode": cfg.group_size is not None,
            "regroup_interval": cfg.regroup_interval is not None,
            "first_as_main": cfg.first_as_main,
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
