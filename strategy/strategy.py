# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Code so clean it squeaks
import asyncio
import random
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from strategy.models import StrategyConfig
from strategy.trading import (
    Order,
    Side,
    TradingClient,
    limit_order_and_wait,
    opposite_side,
    usd_to_qty,
)
from utils import helpers as utils
from utils.logger import logger


@dataclass
class TradeAction:
    """Planned trade for one account."""

    client: TradingClient
    side: Side
    size_usd: Decimal
    qty: Decimal = Decimal(0)
    order: Order | None = None


class DeltaStrategy:
    """
    Delta-neutral strategy that works with any TradingClient.
    Opens opposite positions on multiple accounts.
    """

    def __init__(self, cfg: StrategyConfig, accounts: Sequence[TradingClient]):
        self.cfg = cfg
        self.accounts = list(accounts)
        self.initial_bal = Decimal(0)

    # MARK: Core trading flow

    async def _loop(self):
        # TODO: not sure do we need to close all markets or only markets from config?
        await self.close_all()

        while True:
            try:
                print("-" * 60)
                await self.trade_cycle()

                wait_sec = self.cfg.trade_cooldown.sample()
                logger.info(utils.wait_msg(wait_sec))
                await asyncio.sleep(wait_sec)
            except Exception as e:
                logger.warning(f"Trade cycle failed {type(e)}: {e}")
                await self.close_all()
                break

    async def run(self):
        """Main entry point."""
        if not (2 <= len(self.accounts) <= 5):
            logger.error(f"Accounts for trading must be between 2 and 5, got {len(self.accounts)}")
            exit(1)

        # Warmup all accounts to avoid captcha & check registration before starting trading loop
        await self.warmup()
        bals = await self.get_balances()
        self.initial_bal = sum(bal for _, bal in bals)

        while True:
            try:
                await self._loop()
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

        # Debug trade size calculation
        size_usd = sum(x.size_usd for x in actions)
        rest_sizes = " ".join([str(x.size_usd) for x in actions[1:]])
        rest_sizes = f"{sum(x.size_usd for x in actions[1:])} ({rest_sizes})"
        logger.info(f"Trade {market}: {size_usd} = {actions[0].size_usd} + {rest_sizes}")

        # 3. Set leverage
        await asyncio.gather(*[self._ensure_leverage(acc, market) for acc in self.accounts])

        # 4. Open positions
        await self.open_positions(actions, market)

        # 5. Wait with safety checks
        success = await self.wait_with_checks(actions, market)

        # 6. Close positions
        await self.close_positions(actions, market, use_limit=success and self.cfg.use_limit)

        # 7. Report P/L
        await self.report_pnl(balances)

    async def _ensure_leverage(self, acc: TradingClient, symbol: str) -> None:
        current = await acc.get_leverage(symbol)
        if current is None or current != self.cfg.leverage:
            await acc.set_leverage(symbol, self.cfg.leverage)

    # MARK: Position management

    async def open_positions(self, actions: list[TradeAction], market: str):
        """Open positions. Main account uses limit if configured."""
        if self.cfg.use_limit:
            # Main account: limit order with wait
            main = actions[0]
            main.order = await self._limit_order_and_wait(main.client, market, main.side, main.qty)
            if main.order is None:
                await self.close_all()
                return

            # Rest: market orders
            rest_tasks = [act.client.market_order(market, act.side, act.qty) for act in actions[1:]]
            results = await asyncio.gather(*rest_tasks)
            for act, order in zip(actions[1:], results):
                act.order = order
        else:
            # All market orders
            tasks = [act.client.market_order(market, act.side, act.qty) for act in actions]
            results = await asyncio.gather(*tasks)
            for act, order in zip(actions, results):
                act.order = order

    async def close_positions(self, actions: list[TradeAction], market: str, use_limit=False):
        """Close this cycle's positions on the given market only."""
        if use_limit:
            main = actions[0]
            positions = await main.client.positions()
            for pos in [p for p in positions if p.symbol == market]:
                await self._limit_order_and_wait(
                    main.client,
                    pos.symbol,
                    opposite_side(pos.side),
                    pos.size,
                    reduce_only=True,
                )

        await self.close_all()

    async def _limit_order_and_wait(
        self,
        client: TradingClient,
        symbol: str,
        side: Side,
        qty: Decimal,
        reduce_only=False,
    ) -> Order | None:
        """Place limit order and wait for fill (using strategy config)."""
        return await limit_order_and_wait(
            client,
            symbol,
            side,
            qty,
            reduce_only=reduce_only,
            timeout=self.cfg.limit_wait,
            use_market_fallback=self.cfg.limit_market_fallback,
        )

    # MARK: Safety checks

    async def wait_with_checks(self, actions: list[TradeAction], market: str) -> bool:
        """Wait for duration, checking positions periodically."""
        duration = self.cfg.trade_duration.sample()
        logger.info(utils.wait_msg(duration))

        until = time.time() + duration

        while time.time() < until:
            await asyncio.sleep(min(self.cfg.trade_heartbeat, until - time.time()))

            try:
                if not await self.check_positions(actions, market):
                    return False
            except Exception as e:
                logger.warning(f"Position safety check failed {type(e)}: {e}, continuing wait...")

        return True

    async def check_positions(self, actions: list[TradeAction], market: str) -> bool:
        """Check if positions are within risk limits."""
        for act in actions:
            positions = await act.client.positions()
            market_pos = [p for p in positions if p.symbol == market]

            if len(market_pos) != 1:
                logger.warning(f"{len(market_pos)} positions for {market} on {act.client.name}")
                return False

            pos = market_pos[0]
            if pos.size == 0:
                continue

            price = await act.client.get_price(market)
            entry_cost = pos.size * pos.entry_price
            current_cost = pos.size * price
            roi = (current_cost / entry_cost - 1) * (1 if pos.side == "bid" else -1)

            if abs(roi) >= self.cfg.pnl_limit:
                tmp = f"{roi:.2%} ({entry_cost:.2f} -> {current_cost:.2f})"
                logger.info(f"Position {market} hit stop loss at {tmp}, closing...")
                return False

        return True

    # MARK: Helpers

    async def warmup(self):
        rs = await asyncio.gather(*[a.warmup() for a in self.accounts], return_exceptions=True)
        rs = [a.name for a, r in zip(self.accounts, rs) if isinstance(r, Exception)]
        if rs:
            logger.error(f"Warmup failed: {', '.join(rs)}")
            exit(1)

        rs = await asyncio.gather(*[a.registered() for a in self.accounts], return_exceptions=True)
        rs = [a.name for a, r in zip(self.accounts, rs) if isinstance(r, Exception) or r is False]
        if rs:
            logger.error(f"Not registered: {', '.join(rs)}")
            exit(1)

    async def get_balances(self) -> list[tuple[str, float]]:
        """Get balances for all accounts."""
        bals = await asyncio.gather(*[acc.balance() for acc in self.accounts])
        return [(acc.name, float(b)) for acc, b in zip(self.accounts, bals)]

    def plan_trades(self, balances: list[tuple[str, float]]) -> list[TradeAction] | None:
        """Use find_safe_pair to plan trades."""
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

    async def close_all(self):
        rs1 = await asyncio.gather(*[acc.cancel_all_orders() for acc in self.accounts])
        rs2 = await asyncio.gather(*[acc.close_all_positions() for acc in self.accounts])

        if sum(rs1) + sum(rs2) > 0:
            logger.info(f"Closed {sum(rs1)} orders and {sum(rs2)} positions")

    async def report_pnl(self, was: list[tuple[str, float]]):
        now = await self.get_balances()
        diff_sum = sum(x[1] for x in now) - sum(x[1] for x in was)
        diff_str = [(x[0], x[1] - y[1]) for x, y in zip(now, was)]
        diff_str = " | ".join([f"{name} {diff:+.2f}" for name, diff in diff_str])
        total_pnl = sum(x[1] for x in now) - float(self.initial_bal)
        logger.info(f"Δ {diff_sum:+.2f} ~ {diff_str}; Total P/L: {total_pnl:+.2f}")
