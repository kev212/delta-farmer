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


class Balances:
    def __init__(self, data: dict[str, float]):
        self._data = data

    @property
    def total(self) -> float:
        return sum(self._data.values())

    def items(self) -> list[tuple[str, float]]:
        return list(self._data.items())

    def log(self) -> None:
        parts = " | ".join(f"{name} {bal:.2f}" for name, bal in self._data.items())
        logger.info(f"Balances: {self.total:.2f} = {parts}")

    def log_pnl(self, prev: "Balances", initial_total: float | Decimal) -> float:
        diff_sum = self.total - prev.total
        diffs = " | ".join(f"{x} {self._data[x] - prev._data[x]:+.2f}" for x in self._data)
        total_pnl = self.total - float(initial_total)
        logger.info(f"Δ {diff_sum:+.2f} ~ {diffs}; Total P/L: {total_pnl:+.2f}")
        return diff_sum


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
        self.initial_bal: float = 0.0

    # MARK: Core trading flow

    async def _wait(self, wait_sec: float):
        logger.info(utils.wait_msg(wait_sec))
        await utils.interruptible_sleep(wait_sec, self.stop_event)

    async def run(self):
        bals = await self.get_balances(self.accounts)
        self.initial_bal = bals.total
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
                if self.cfg.max_failures > 0 and failures >= self.cfg.max_failures:
                    logger.opt(exception=True).error("Too many consecutive failures, stopping")
                    await tg.on_crash(f"{type(e).__name__}: {e}")
                    # TODO: return exits only this group (others keep running); raise propagates to
                    # CLI and prints ugly traceback; SystemExit(1) kills the whole process cleanly.
                    # Decide which behaviour is correct for multi-group mode.
                    return

                wait_sec = min(3 * (2 ** (failures - 1)), 60) * 60  # 3m→6m→12m→24m→48m→60m
                wait_str = utils.format_duration(wait_sec)
                msg = f"Cycle failed ({failures}) {type(e).__name__}: {e}, retrying in {wait_str}"
                logger.warning(msg)
                await tg.on_error(f"{type(e).__name__}: {e}", failures, wait_sec)
                await self._wait(wait_sec)

    async def trade_cycle(self):
        """Run one full trade cycle across the selected symbols."""
        # 1. Get balances
        accounts = self.get_ordered_accounts()
        was_bals = await self.get_balances(accounts)
        was_bals.log()

        # 2. Pick symbols and build full plan
        symbols = random.sample(self.cfg.symbols, self.cfg.symbols_per_trade)
        total_usd = self.get_trade_size(was_bals)
        symbol_actions = await plan_symbol_actions(
            accounts, symbols, total_usd, self.cfg.leverage, was_bals.items()
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
        now_bals = await self.get_balances(accounts)
        pnl = now_bals.log_pnl(was_bals, self.initial_bal)
        dur = time.time() - stime
        await tg.on_trade_stop(pnl, dur, float(total_size), now_bals.items(), msg_id)

    # MARK: Helpers

    @retry(max_attempts=3, delay=2.0)
    async def get_balances(self, accs: list[TradingClient]) -> Balances:
        vals = await asyncio.gather(*[acc.balance() for acc in accs])
        return Balances({acc.name: float(v) for acc, v in zip(accs, vals)})

    def get_trade_size(self, bals: Balances) -> Decimal:
        if self.cfg.trade_size_pct is not None:
            return calc_total_from_pct(bals.items(), self.cfg.leverage, self.cfg.trade_size_pct)
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
