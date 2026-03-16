# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Code so clean it squeaks
import asyncio
import random
import time
from decimal import Decimal
from typing import Sequence

from lib import telegram as tg
from lib import utils
from lib.decorators import retry
from lib.logger import logger

from .execution import (
    check_min_trade_sizes,
    close_all,
    close_symbol_positions,
    ensure_leverage,
    hold_positions,
    open_positions,
)
from .models import StrategyConfig, TradeAction, TradingClient, usd_to_qty
from .planner import calc_total_from_pct, plan_symbol_actions


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

    MAX_FAILURES = 5

    async def _wait(self, wait_sec: float):
        logger.info(utils.wait_msg(wait_sec))
        await utils.interruptible_sleep(wait_sec, self.stop_event)

    async def run(self):
        bals = await self.get_balances()
        self.initial_bal = sum(bal for _, bal in bals)
        await close_all(self.accounts)  # clean up leftovers from a previous run

        failures = 0
        while True:
            try:
                print("-" * 60) if not self.cfg.group_size else None
                await self.trade_cycle()
                failures = 0
                await self._wait(self.cfg.trade_cooldown.sample())
            except asyncio.CancelledError:  # stop_event triggered, time to exit
                await close_all(self.accounts)
                return
            except Exception as e:
                await close_all(self.accounts)

                failures += 1
                if failures >= self.MAX_FAILURES:
                    logger.error("Too many consecutive failures, stopping strategy")
                    await tg.on_crash(f"{type(e).__name__}: {e}")
                    raise

                msg = f"Cycle failed ({failures}/{self.MAX_FAILURES}) {type(e).__name__}: {e}"
                logger.warning(msg)
                await tg.on_error(f"{type(e).__name__}: {e}", failures, self.MAX_FAILURES)
                await self._wait(60 * 3)  # wait a bit before retrying after a failure

    async def trade_cycle(self):
        """Run one full trade cycle across the selected symbols."""
        # 1. Get balances
        accounts = self.get_ordered_accounts()
        balances = await self.get_balances(accounts)
        bal_str = " | ".join([f"{name} {bal:.2f}" for name, bal in balances])
        bal_str = f"{sum(bal for _, bal in balances):.2f} = " + bal_str
        logger.info(f"Balances: {bal_str}")

        # 2. Pick symbols and build full plan
        symbols = random.sample(self.cfg.symbols, self.cfg.symbols_per_trade)
        total_usd = self.get_trade_size(balances)
        symbol_actions = await plan_symbol_actions(
            accounts, symbols, total_usd, self.cfg.leverage, balances
        )
        if symbol_actions is None:
            logger.error("No valid account combination found for trading.")
            return

        # 3. Check min trade size per symbol
        for symbol, actions in symbol_actions.items():
            await check_min_trade_sizes(actions, symbol)
            size_usd = sum(x.size_usd for x in actions)
            rest_sizes = " ".join(str(x.size_usd) for x in actions[1:])
            rest_sizes = f"{sum(x.size_usd for x in actions[1:])} ({rest_sizes})"
            logger.info(f"Trade {symbol}: {size_usd} = {actions[0].size_usd} + {rest_sizes}")

        # 4. Open positions symbol by symbol
        total_size = float(sum(sum(a.size_usd for a in acts) for acts in symbol_actions.values()))
        acc_names = [x.client.name for acts in symbol_actions.values() for x in acts]
        acc_names = list(dict.fromkeys(acc_names))  # unique while preserving order
        msg_id = await tg.on_trade_start(list(symbol_actions.keys()), total_size, acc_names)
        stime = time.time()

        for symbol, actions in symbol_actions.items():
            await self.open_symbol_positions(symbol, actions)

        # 5. Wait with safety checks
        success = await hold_positions(symbol_actions, self.cfg, self.stop_event)

        # 6. Close positions symbol by symbol
        for symbol, actions in symbol_actions.items():
            acts = [act.client for act in actions]
            use_limit = self.cfg.use_limit and success
            await close_symbol_positions(acts, symbol, self.cfg, use_limit=use_limit)

        # 7. Report P/L
        pnl, now_bals = await self.report_pnl(balances)
        await tg.on_trade_stop(pnl, time.time() - stime, now_bals, reply_to=msg_id)
        tg.on_trade(total_size, pnl)

    # MARK: Helpers

    @retry(max_attempts=3, delay=2.0)
    async def get_balances(
        self, accs: list[TradingClient] | None = None
    ) -> list[tuple[str, float]]:
        accs = accs or self.accounts
        bals = await asyncio.gather(*[acc.balance() for acc in accs])
        return [(acc.name, float(b)) for acc, b in zip(accs, bals)]

    def get_trade_size(self, ordered_balances: list[tuple[str, float]]) -> Decimal:
        if self.cfg.trade_size_pct is not None:
            return calc_total_from_pct(ordered_balances, self.cfg.leverage, self.cfg.trade_size_pct)
        return Decimal(str(self.cfg.trade_size_usd.sample()))  # type: ignore[union-attr]

    def get_ordered_accounts(self) -> list[TradingClient]:
        return (
            self.accounts[:1] + utils.shuffle(self.accounts[1:])
            if self.cfg.first_as_prime
            else utils.shuffle(self.accounts)
        )

    async def open_symbol_positions(self, symbol: str, actions: list[TradeAction]) -> None:
        price = await actions[0].client.get_price(symbol)
        lot_size = await actions[0].client.get_lot_size(symbol)

        for act in actions:
            act.qty = usd_to_qty(act.size_usd, price, lot_size)

        await ensure_leverage([act.client for act in actions], symbol, self.cfg.leverage)
        await open_positions(actions, symbol, self.cfg)
        failed = [act.client.name for act in actions if act.order is None]
        if failed:
            raise RuntimeError(f"Failed to open {symbol} on: {', '.join(failed)}")

    async def report_pnl(
        self, was: list[tuple[str, float]]
    ) -> tuple[float, list[tuple[str, float]]]:
        now = await self.get_balances()
        diff_sum = sum(x[1] for x in now) - sum(x[1] for x in was)
        diff_str = [(x[0], x[1] - y[1]) for x, y in zip(now, was)]
        diff_str = " | ".join([f"{name} {diff:+.2f}" for name, diff in diff_str])
        total_pnl = sum(x[1] for x in now) - float(self.initial_bal)
        logger.info(f"Δ {diff_sum:+.2f} ~ {diff_str}; Total P/L: {total_pnl:+.2f}")
        return diff_sum, now
