# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Warning: May cause enlightenment
import asyncio
import time
from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from lib.logger import logger
from lib.utils import round_to_tick_size

Side = Literal["bid", "ask"]


class OrderStatus(StrEnum):
    OPEN = "open"  # active on book (new / pending / partial)
    FILLED = "filled"  # fully filled
    CANCELED = "canceled"  # canceled, expired, or rejected


class Position(BaseModel):
    """Unified position model across all exchanges."""

    id: str
    symbol: str
    side: Side
    size: Decimal  # always positive, in base asset
    entry_price: Decimal
    unrealized_pnl: Decimal = Decimal(0)


class Order(BaseModel):
    """Unified order model across all exchanges."""

    id: str
    symbol: str
    side: Side
    size: Decimal  # total size
    filled: Decimal  # filled size
    price: Decimal | None  # None for market orders
    status: OrderStatus
    reduce_only: bool = False


class ProfileInfo(BaseModel):
    """Account profile summary for reporting (info command)."""

    addr: str  # pre-formatted display address
    balance: Decimal
    volume: Decimal
    pnl: Decimal  # net realized PnL as trading pnl - fees - funding
    points: Decimal


@runtime_checkable
class TradingClient(Protocol):
    """Protocol for all trading clients."""

    @property
    def name(self) -> str: ...

    # Lifecycle
    async def warmup(self) -> None: ...

    # Account
    async def balance(self) -> Decimal: ...

    # Price & conversion
    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]: ...  # (best_bid, best_ask)
    async def get_price(self, symbol: str) -> Decimal: ...
    async def get_lot_size(self, symbol: str) -> Decimal:
        """Minimum quantity increment (e.g. 0.0001 BTC)."""
        ...

    async def get_tick_size(self, symbol: str) -> Decimal:
        """Minimum price increment (e.g. $1 for BTC, $0.01 for smaller assets)."""
        ...

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        """Minimum notional trade value in USD. Hardcoded per exchange; TODO: derive from API."""
        ...

    # Positions
    async def positions(self) -> list[Position]: ...
    async def close_position(self, position: Position) -> bool: ...

    # Orders - always work with qty (base asset quantity)
    async def market_order(
        self, symbol: str, side: Side, qty: Decimal, reduce_only=False
    ) -> Order: ...

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order: ...

    async def cancel_order(self, order: Order) -> bool: ...
    async def get_order(self, order_id: str) -> Order | None: ...

    # Cleanup
    async def cancel_all_orders(self) -> int: ...
    async def close_all_positions(self) -> int: ...

    # Account checks
    async def registered(self) -> bool: ...

    # Market discovery
    async def get_symbols(self) -> list[str]:
        """All tradable symbols, sorted by liquidity/relevance (best candidates first)."""
        ...

    # Leverage
    async def get_leverage(self, symbol: str) -> int | None: ...
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...


# Utility functions (not in protocol)


async def close_all(clients: Sequence[TradingClient]) -> None:
    """Warmup, cancel all orders, and close all positions for a list of clients."""
    for client in clients:
        await client.warmup()
        count1 = await client.cancel_all_orders()
        count2 = await client.close_all_positions()
        logger.info(f"{client.name}: Canceled {count1} orders, closed {count2} positions")


def usd_to_qty(usd: Decimal, price: Decimal, lot_size: Decimal) -> Decimal:
    """Convert USD amount to quantity, rounded to lot size."""
    qty = usd / price
    return round_to_tick_size(qty, lot_size)


def opposite_side(side: Side) -> Side:
    """Return the opposite side."""
    return "ask" if side == "bid" else "bid"


async def limit_order_and_wait(
    client: TradingClient,
    symbol: str,
    side: Side,
    qty: Decimal,
    price: Decimal | None = None,
    reduce_only=False,
    timeout=60,
    use_market_fallback=True,
) -> Order | None:
    """Place limit order and wait for fill with optional market fallback."""
    if price is None:
        tick_size = await client.get_tick_size(symbol)
        bid, ask = await client.get_bbo(symbol)
        raw_price = bid if side == "bid" else ask
        price = round_to_tick_size(raw_price, tick_size)

    logger.debug(f"Limit {side} {qty} {symbol} @ {price} ({client.name})")
    order = await client.limit_order(symbol, side, qty, price, reduce_only)
    order_id = order.id
    started_at, filled_since = time.time(), None

    while True:
        await asyncio.sleep(3)
        order = await client.get_order(order_id)
        if order is None:
            continue  # still open or archive lag — keep polling

        if order.status == OrderStatus.FILLED:
            logger.debug(f"Limit order filled in {time.time() - started_at:.1f}s")
            return order

        if order.status == OrderStatus.CANCELED:
            logger.debug(f"Limit order canceled after {time.time() - started_at:.1f}s")
            return None

        if order.filled > 0 and filled_since is None:
            filled_since = time.time()

        check_time = filled_since or started_at
        if (time.time() - check_time) > timeout:
            logger.debug(f"Limit order timeout after {timeout}s")
            await client.cancel_order(order)
            remaining = order.size - order.filled
            if use_market_fallback and remaining > 0:
                logger.debug(f"Market fallback for {remaining}")
                return await client.market_order(symbol, side, remaining, reduce_only)
            return None
