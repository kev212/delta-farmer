# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Code so clean it squeaks
import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from lib import utils
from lib.logger import logger
from strategy.models import StrategyConfig
from strategy.trading import Order, Side, TradingClient, limit_order_and_wait, opposite_side


@dataclass
class TradeAction:
    """Planned trade for one account."""

    client: TradingClient
    side: Side
    size_usd: Decimal
    qty: Decimal = Decimal(0)
    order: Order | None = None


# MARK: Execution primitives


async def ensure_leverage(accounts: Sequence[TradingClient], market: str, leverage: int) -> None:
    async def _ensure(acc: TradingClient) -> None:
        current = await acc.get_leverage(market)
        if current is None or current != leverage:
            await acc.set_leverage(market, leverage)

    await asyncio.gather(*[_ensure(acc) for acc in accounts])


async def close_all(accounts: Sequence[TradingClient]) -> None:
    rs1 = await asyncio.gather(*[acc.cancel_all_orders() for acc in accounts])
    rs2 = await asyncio.gather(*[acc.close_all_positions() for acc in accounts])

    if sum(rs1) + sum(rs2) > 0:
        logger.info(f"Closed {sum(rs1)} orders and {sum(rs2)} positions")


async def _limit_order_and_wait(
    client: TradingClient,
    symbol: str,
    side: Side,
    qty: Decimal,
    cfg: StrategyConfig,
    reduce_only: bool = False,
) -> Order | None:
    return await limit_order_and_wait(
        client,
        symbol,
        side,
        qty,
        reduce_only=reduce_only,
        timeout=cfg.limit_wait,
        use_market_fallback=cfg.limit_market_fallback,
    )


async def check_min_trade_sizes(actions: list[TradeAction], market: str) -> None:
    """Raise if any account's trade size is below the exchange minimum."""
    min_usds = await asyncio.gather(*[act.client.get_min_trade_usd(market) for act in actions])
    failed = [
        (act.client.name, act.size_usd, min_usd)
        for act, min_usd in zip(actions, min_usds)
        if act.size_usd < min_usd
    ]
    if not failed:
        return
    for name, size, min_usd in failed:
        logger.warning(f"{name}: {size:.2f} < min {min_usd:.2f} USD for {market}")
    names = ", ".join(name for name, _, _ in failed)
    raise RuntimeError(f"Trade size below minimum for: {names}")


async def open_positions(actions: list[TradeAction], market: str, cfg: StrategyConfig) -> None:
    """Open positions. Main account uses limit if configured."""
    if cfg.use_limit:
        main = actions[0]
        main.order = await _limit_order_and_wait(main.client, market, main.side, main.qty, cfg)
        if main.order is None:
            await close_all([act.client for act in actions])
            return

        actions = actions[1:]

    results = await asyncio.gather(
        *[act.client.market_order(market, act.side, act.qty) for act in actions]
    )
    for act, order in zip(actions, results):
        act.order = order
        log = logger.bind(account=act.client.name)
        log.debug(f"Market {act.side} {act.qty} {market} filled")


async def close_positions(
    actions: list[TradeAction],
    market: str,
    accounts: Sequence[TradingClient],
    cfg: StrategyConfig,
    use_limit: bool = False,
) -> None:
    """Close this cycle's positions on the given market only."""
    if use_limit:
        main = actions[0]
        positions = await main.client.positions()
        for pos in [p for p in positions if p.symbol == market]:
            await _limit_order_and_wait(
                main.client, pos.symbol, opposite_side(pos.side), pos.size, cfg, reduce_only=True
            )

    await close_all(accounts)


async def check_positions(actions: list[TradeAction], market: str, pnl_limit: float) -> bool:
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

        if abs(roi) >= pnl_limit:
            tmp = f"{roi:.2%} ({entry_cost:.2f} -> {current_cost:.2f})"
            logger.info(f"Position {market} hit stop loss at {tmp}, closing...")
            return False

    return True


async def wait_with_checks(
    actions: list[TradeAction],
    market: str,
    cfg: StrategyConfig,
    stop_event: asyncio.Event | None = None,
) -> bool:
    """Wait for trade duration, periodically checking positions."""
    duration = cfg.trade_duration.sample()
    logger.info(utils.wait_msg(duration))

    until = time.time() + duration

    while time.time() < until:
        if stop_event and stop_event.is_set():
            logger.info("Stop event received, exiting early")
            return False

        sleep_for = min(cfg.trade_heartbeat, until - time.time())
        await asyncio.sleep(max(0, sleep_for))

        try:
            if not await check_positions(actions, market, cfg.pnl_limit):
                return False
        except Exception as e:
            logger.warning(f"Position safety check failed {type(e)}: {e}, continuing wait...")

    return True
