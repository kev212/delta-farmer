# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Warning: May cause enlightenment
import asyncio
import time
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from utils.helpers import round_to_tick_size
from utils.logger import logger

Side = Literal["bid", "ask"]


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
    status: str  # open, filled, cancelled, etc. # TODO: should be known set of common statuses
    reduce_only: bool = False


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
    async def get_price(self, symbol: str) -> Decimal: ...
    async def get_lot_size(self, symbol: str) -> Decimal:
        """Minimum quantity increment (e.g. 0.0001 BTC)."""
        ...

    async def get_tick_size(self, symbol: str) -> Decimal:
        """Minimum price increment (e.g. $1 for BTC, $0.01 for smaller assets)."""
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
    slippage=Decimal("0.0005"),
) -> Order | None:
    """Place limit order and wait for fill with optional market fallback."""
    if price is None:
        tick_size = await client.get_tick_size(symbol)
        last_price = await client.get_price(symbol)
        slip = (1 - slippage) if side == "bid" else (1 + slippage)
        price = round_to_tick_size(last_price * slip, tick_size)

    order = await client.limit_order(symbol, side, qty, price, reduce_only)
    order_id = order.id
    started_at, filled_since = time.time(), None

    while True:
        await asyncio.sleep(3)
        order = await client.get_order(order_id)
        if order is None:
            logger.warning(f"Order {order_id} not found")
            return None

        if order.status.lower() == "filled":
            logger.info(f"Limit order filled in {time.time() - started_at:.1f}s")
            return order

        if order.status.lower() in ("cancelled", "canceled", "rejected"):
            logger.info(f"Limit order {order.status}")
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
