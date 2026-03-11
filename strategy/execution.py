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
from strategy.trading import (
    Order,
    Position,
    Side,
    TradingClient,
    fill_limit_order,
    opposite_side,
)


@dataclass
class TradeAction:
    """Planned trade for one account."""

    client: TradingClient
    side: Side
    size_usd: Decimal
    qty: Decimal = Decimal(0)
    order: Order | None = None


# MARK: Execution primitives


async def ensure_leverage(accounts: Sequence[TradingClient], symbol: str, leverage: int) -> None:
    async def _ensure(acc: TradingClient) -> None:
        current = await acc.get_leverage(symbol)
        if current is None or current != leverage:
            await acc.set_leverage(symbol, leverage)

    await asyncio.gather(*[_ensure(acc) for acc in accounts])


async def close_all(accs: Sequence[TradingClient], _attempts: int = 3) -> None:
    """Best-effort cleanup — retries on failure, never raises. cancel/close are idempotent."""
    n_failed = 0
    for attempt in range(_attempts):
        rs1 = await asyncio.gather(*[a.cancel_all_orders() for a in accs], return_exceptions=True)
        rs2 = await asyncio.gather(*[a.close_all_positions() for a in accs], return_exceptions=True)

        n_orders = sum(r for r in rs1 if isinstance(r, int))
        n_positions = sum(r for r in rs2 if isinstance(r, int))
        n_failed = sum(1 for r in [*rs1, *rs2] if isinstance(r, Exception))
        if n_orders + n_positions > 0:
            logger.info(f"Closed {n_orders} orders and {n_positions} positions")
        if not n_failed:
            return

        if attempt < _attempts - 1:
            logger.warning(f"close_all: {n_failed} failed, retrying ({attempt + 1}/{_attempts})...")
            await asyncio.sleep(2.0 * 2**attempt)

    logger.warning(f"close_all: still {n_failed} account(s) failed after {_attempts} attempts")


async def _fill_limit_order(
    client: TradingClient,
    symbol: str,
    side: Side,
    qty: Decimal,
    cfg: StrategyConfig,
    reduce_only: bool = False,
) -> Order | None:
    return await fill_limit_order(
        client,
        symbol,
        side,
        qty,
        reduce_only=reduce_only,
        timeout=cfg.limit_wait,
        use_market_fallback=cfg.limit_market_fallback,
    )


async def check_min_trade_sizes(actions: list[TradeAction], symbol: str) -> None:
    """Raise if any account's trade size is below the exchange minimum."""
    min_usds = await asyncio.gather(*[act.client.get_min_trade_usd(symbol) for act in actions])
    failed = [
        (act.client.name, act.size_usd, min_usd)
        for act, min_usd in zip(actions, min_usds)
        if act.size_usd < min_usd
    ]
    if not failed:
        return
    for name, size, min_usd in failed:
        logger.warning(f"{name}: {size:.2f} < min {min_usd:.2f} USD for {symbol}")
    names = ", ".join(name for name, _, _ in failed)
    raise RuntimeError(f"Trade size below minimum for: {names}")


async def open_positions(acts: list[TradeAction], symbol: str, cfg: StrategyConfig) -> None:
    """Open positions. Main account uses limit if configured."""
    all_acts = acts
    if cfg.use_limit:
        main, acts = acts[0], acts[1:]
        main.order = await _fill_limit_order(main.client, symbol, main.side, main.qty, cfg)
        if main.order is None:
            await close_all([act.client for act in all_acts])
            return

    rs = await asyncio.gather(*[act.client.market_order(symbol, act.side, act.qty) for act in acts])
    for act, order in zip(acts, rs):
        act.order = order
        log = logger.bind(account=act.client.name)
        log.debug(f"Market {act.side} {act.qty} {symbol} filled")


async def close_symbol_positions(
    accs: list[TradingClient], symbol: str, cfg: StrategyConfig, use_limit=False
) -> None:
    """Close this cycle's positions on the given symbol only."""
    assert len(set(acc.name for acc in accs)) == len(accs), "Duplicate accounts in close_positions"

    if use_limit:
        main, accs = accs[0], accs[1:]
        positions = await main.positions()
        positions = [p for p in positions if p.symbol == symbol]
        for pos in positions:
            side = opposite_side(pos.side)
            await _fill_limit_order(main, pos.symbol, side, pos.size, cfg, reduce_only=True)

    for acc in accs:
        positions = await acc.positions()
        positions = [p for p in positions if p.symbol == symbol]
        for pos in positions:
            await acc.close_position(pos)
            log = logger.bind(account=acc.name)
            log.debug(f"Closed {pos.size} {symbol} with market order")


async def _position_state(client: TradingClient, symbol: str) -> Position | None:
    positions = await client.positions()
    symbol_positions = [p for p in positions if p.symbol == symbol]

    if len(symbol_positions) != 1:
        logger.warning(f"{len(symbol_positions)} positions for {symbol} on {client.name}")
        return None

    return symbol_positions[0]


async def _position_roi(
    client: TradingClient, symbol: str
) -> tuple[Decimal, Decimal, Decimal] | None:
    pos = await _position_state(client, symbol)
    if pos is None:
        return None
    if pos.size == 0:
        return Decimal(0), Decimal(0), Decimal(0)

    price = await client.get_price(symbol)
    entry_cost = pos.size * pos.entry_price
    current_cost = pos.size * price
    sign = Decimal(1) if pos.side == "bid" else Decimal(-1)
    pnl = (current_cost - entry_cost) * sign
    roi = pnl / entry_cost
    return roi, pnl, entry_cost


async def positions_within_limits(
    symbol_actions: dict[str, list[TradeAction]],
    position_roi_limit: Decimal,
    combined_roi_limit: Decimal,
) -> bool:
    total_pnl = Decimal(0)
    total_entry_cost = Decimal(0)
    checks = [(symbol, act) for symbol, actions in symbol_actions.items() for act in actions]
    states = await asyncio.gather(*[_position_roi(act.client, symbol) for symbol, act in checks])

    for (symbol, act), state in zip(checks, states):
        if state is None:
            return False

        roi, pnl, entry_cost = state
        if abs(roi) >= position_roi_limit:
            logger.info(f"Position {symbol} on {act.client.name} hit {roi:.2%}, closing...")
            return False

        total_pnl += pnl
        total_entry_cost += entry_cost

    if total_entry_cost == 0:
        return True

    combined_roi = total_pnl / total_entry_cost
    if abs(combined_roi) >= combined_roi_limit:
        logger.info(f"Combined ROI hit {combined_roi:.2%}, closing...")
        return False

    return True


async def hold_positions(
    symbol_actions: dict[str, list[TradeAction]],
    cfg: StrategyConfig,
    stop_event: asyncio.Event | None = None,
) -> bool:
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
            if not await positions_within_limits(
                symbol_actions,
                Decimal(str(cfg.position_roi_limit)),
                Decimal(str(cfg.combined_roi_limit)),
            ):
                return False
        except Exception as e:
            logger.warning(f"Position safety check failed {type(e)}: {e}, continuing wait...")

    return True
