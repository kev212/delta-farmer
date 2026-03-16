# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Warning: May cause enlightenment
import sys
import tomllib
import warnings
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, Type, TypeVar, runtime_checkable

from pydantic import BaseModel, Field, ValidationError, model_validator

from lib.models import DurationSec, SizeRange, TgConfig, TimeRange
from lib.utils import round_to_tick_size

# MARK: Trading types

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
    ref_code: str | None = None


# MARK: Protocol


@runtime_checkable
class TradingClient(Protocol):
    """Protocol for all trading clients."""

    exchange: str

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


# MARK: Utilities


def usd_to_qty(usd: Decimal, price: Decimal, lot_size: Decimal) -> Decimal:
    """Convert USD amount to quantity, rounded to lot size."""
    qty = usd / price
    return round_to_tick_size(qty, lot_size)


def opposite_side(side: Side) -> Side:
    """Return the opposite side."""
    return "ask" if side == "bid" else "bid"


# MARK: Trade plan


@dataclass
class TradeAction:
    """Planned trade for one account."""

    client: TradingClient
    side: Side
    size_usd: Decimal
    qty: Decimal = Decimal(0)
    order: Order | None = None


# MARK: Strategy config

ConfigT = TypeVar("ConfigT", bound=BaseModel)


class StrategyConfig(BaseModel):
    """Base config for trading strategies."""

    symbols: list[str] = Field(..., min_length=1)
    symbols_per_trade: int = Field(1, gt=0, le=4)
    leverage: int = Field(10, gt=0, lt=50)
    trade_size_usd: SizeRange | None = None
    trade_size_pct: float | None = Field(None, ge=0.01, le=1.0)
    trade_duration: TimeRange
    trade_cooldown: TimeRange
    trade_heartbeat: DurationSec = DurationSec("15s")
    position_roi_limit: float = Field(0.8, gt=0, lt=1)
    combined_roi_limit: float = Field(0.1, gt=0, lt=1)
    use_limit: bool = False
    limit_wait: DurationSec = DurationSec("90s")
    limit_market_fallback: bool = True
    first_as_prime: bool = False
    group_size: int | None = Field(None, ge=2, le=5)
    regroup_interval: DurationSec | None = None
    telegram: TgConfig = Field(default_factory=lambda: TgConfig())

    @model_validator(mode="before")
    @classmethod
    def _before(cls, values):
        if isinstance(values, dict):
            if "symbols" in values and "markets" in values:
                raise ValueError("Use `symbols` only; replace legacy `markets` with `symbols`")
            if "markets" in values:
                warnings.warn("`markets` is deprecated, use `symbols` instead")
                values["symbols"] = values.pop("markets")
            if "first_as_main" in values:
                warnings.warn("`first_as_main` is deprecated, use `first_as_prime` instead")
                values["first_as_prime"] = values.pop("first_as_main")
        return values


def load_config(config_cls: Type[ConfigT], filepath: str) -> ConfigT:
    """Load and validate a Pydantic config from a TOML file with user-friendly errors."""
    try:
        with open(filepath, "rb") as fp:
            obj = tomllib.load(fp)
    except FileNotFoundError:
        raise SystemExit(f"❌ Config file not found: {filepath}")
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"❌ Invalid TOML syntax in {filepath}: {e}")

    try:
        return config_cls.model_validate(obj)
    except ValidationError as e:
        print(f"❌ Config validation failed for {filepath}\n", file=sys.stderr)
        errors = []
        for err in e.errors():
            field = ".".join(str(x) for x in err["loc"])
            msg = err["msg"]
            errors.append(f"  • {field}: {msg}")
        print("\n".join(errors), file=sys.stderr)
        print(f"\n💡 Fix the errors above in {filepath}", file=sys.stderr)
        raise SystemExit(1)
