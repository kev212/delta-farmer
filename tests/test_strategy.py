"""Tests for strategy execution and orchestration behavior."""

import asyncio
from decimal import Decimal

import pytest

from strategy.delta import DeltaStrategy
from strategy.execution import (
    TradeAction,
    check_min_trade_sizes,
    check_positions,
    ensure_leverage,
    open_positions,
    wait_with_checks,
)
from strategy.models import StrategyConfig
from strategy.trading import Order, OrderStatus, Position, Side, limit_order_and_wait


async def _instant_sleep(_):
    """Replace asyncio.sleep with a no-op for fast tests."""


# MARK: Helpers


def make_order(id: str, symbol: str, side: Side, qty: Decimal) -> Order:
    return Order(
        id=id,
        symbol=symbol,
        side=side,
        size=qty,
        filled=qty,
        price=Decimal("50000"),
        status=OrderStatus.FILLED,
    )


def make_position(symbol: str, side: Side, size: str, entry: str = "50000") -> Position:
    return Position(
        id="p1", symbol=symbol, side=side, size=Decimal(size), entry_price=Decimal(entry)
    )


def make_cfg(**kw) -> StrategyConfig:
    return StrategyConfig.model_validate(
        {
            "markets": ["BTC"],
            "leverage": 10,
            "trade_size_usd": [100, 100],
            "trade_duration": [1, 1],
            "trade_cooldown": [1, 1],
            "trade_heartbeat": 1,
            "pnl_limit": 0.25,
            **kw,
        }
    )


def make_action(client, side: Side, qty: str = "0.002") -> TradeAction:
    return TradeAction(client=client, side=side, size_usd=Decimal("100"), qty=Decimal(qty))


class MockClient:
    """Minimal TradingClient that records calls and returns controllable values."""

    exchange = "mock"

    def __init__(self, name: str, balance: float = 1000, side: Side = "bid", price: float = 50000):
        self._name = name
        self._balance = Decimal(str(balance))
        self._side = side
        self._price = Decimal(str(price))
        self._positions: list[Position] | None = None  # None = default (one BTC position)
        self.calls: list[str] = []
        self._leverage: int | None = None
        self._min_trade_usd: Decimal = Decimal(10)

    @property
    def name(self) -> str:
        return self._name

    def _rec(self, m: str):
        self.calls.append(m)

    async def warmup(self):
        self._rec("warmup")

    async def registered(self):
        return True

    async def balance(self):
        self._rec("balance")
        return self._balance

    async def get_price(self, s):
        self._rec("get_price")
        return self._price

    async def get_bbo(self, s):
        return Decimal("49999"), Decimal("50001")

    async def get_lot_size(self, s):
        return Decimal("0.001")

    async def get_tick_size(self, s):
        return Decimal("1")

    async def get_min_trade_usd(self, s):
        return self._min_trade_usd

    async def get_leverage(self, s):
        self._rec("get_leverage")
        return self._leverage

    async def set_leverage(self, s, lev):
        self._rec("set_leverage")
        self._leverage = lev

    async def positions(self):
        self._rec("positions")
        return (
            self._positions
            if self._positions is not None
            else [make_position("BTC", self._side, "0.002")]
        )

    async def close_position(self, p):
        self._rec("close_position")
        return True

    async def market_order(self, s, side, qty, reduce_only=False):
        self._rec("market_order")
        return make_order("ord-m", s, side, qty)

    async def limit_order(self, s, side, qty, price, reduce_only=False):
        self._rec("limit_order")
        return make_order("ord-l", s, side, qty)

    async def cancel_order(self, o):
        self._rec("cancel_order")
        return True

    async def get_order(self, oid):
        self._rec("get_order")
        return None

    async def cancel_all_orders(self):
        self._rec("cancel_all_orders")
        return 0

    async def close_all_positions(self):
        self._rec("close_all_positions")
        return 1

    async def get_symbols(self):
        return ["BTC"]


# MARK: ensure_leverage


async def test_leverage_set_when_none():
    """set_leverage called when account has no leverage configured."""
    a = MockClient("a")
    await ensure_leverage([a], "BTC", 10)
    assert "set_leverage" in a.calls


async def test_leverage_set_when_wrong():
    """set_leverage called when current leverage differs from target."""
    a = MockClient("a")
    a._leverage = 5
    await ensure_leverage([a], "BTC", 10)
    assert "set_leverage" in a.calls


async def test_leverage_skipped_when_correct():
    """set_leverage NOT called when leverage is already correct."""
    a = MockClient("a")
    a._leverage = 10
    await ensure_leverage([a], "BTC", 10)
    assert "set_leverage" not in a.calls


# MARK: check_positions


async def test_check_within_pnl_limit():
    """No price change → roi=0, returns True."""
    a = MockClient("a", price=50000)
    actions = [make_action(a, "bid")]
    assert await check_positions(actions, "BTC", 0.25) is True


async def test_check_bid_loss_exceeds_limit():
    """Long position loses 40% (price drop) → exceeds 25% limit → False."""
    a = MockClient("a", price=30000)  # entry=50000, now 30000 → -40% on long
    actions = [make_action(a, "bid")]
    assert await check_positions(actions, "BTC", 0.25) is False


async def test_check_ask_loss_exceeds_limit():
    """Short position loses 40% (price rise) → exceeds 25% limit → False."""
    a = MockClient("a", price=70000)  # entry=50000, now 70000 → -40% on short
    actions = [make_action(a, "ask")]
    assert await check_positions(actions, "BTC", 0.25) is False


async def test_check_missing_position():
    """0 positions on market → unexpected state → returns False."""
    a = MockClient("a")
    a._positions = []
    actions = [make_action(a, "bid")]
    assert await check_positions(actions, "BTC", 0.25) is False


# MARK: open_positions


async def test_open_market_mode():
    """Market mode: all clients receive market_order, none receive limit_order."""
    a, b = MockClient("a"), MockClient("b", side="ask")
    actions = [make_action(a, "bid"), make_action(b, "ask")]
    await open_positions(actions, "BTC", make_cfg(use_limit=False))
    assert a.calls.count("market_order") == 1
    assert b.calls.count("market_order") == 1
    assert "limit_order" not in a.calls


async def test_open_limit_mode_fills(monkeypatch):
    """Limit mode: main uses limit order, rest use market orders."""
    a, b = MockClient("a"), MockClient("b", side="ask")
    filled = make_order("ord-l", "BTC", "bid", Decimal("0.002"))

    async def fake_limit(*args, **kw):
        return filled

    monkeypatch.setattr("strategy.execution._limit_order_and_wait", fake_limit)
    actions = [make_action(a, "bid"), make_action(b, "ask")]
    await open_positions(actions, "BTC", make_cfg(use_limit=True))
    assert "market_order" not in a.calls  # main doesn't use market
    assert b.calls.count("market_order") == 1  # rest use market


async def test_open_limit_mode_fails_aborts(monkeypatch):
    """If limit order for main fails (None), no market orders placed for rest."""
    a, b = MockClient("a"), MockClient("b", side="ask")

    async def fake_limit_fail(*args, **kw):
        return None

    monkeypatch.setattr("strategy.execution._limit_order_and_wait", fake_limit_fail)
    actions = [make_action(a, "bid"), make_action(b, "ask")]
    await open_positions(actions, "BTC", make_cfg(use_limit=True))
    assert "market_order" not in b.calls  # rest NOT opened
    assert "cancel_all_orders" in a.calls  # cleanup triggered


# MARK: wait_with_checks


async def test_wait_stop_event_exits_early():
    """Stop event set before wait → returns False immediately."""
    a = MockClient("a")
    stop = asyncio.Event()
    stop.set()
    actions = [make_action(a, "bid")]
    result = await wait_with_checks(actions, "BTC", make_cfg(trade_duration=[5, 5]), stop)
    assert result is False
    assert "positions" not in a.calls  # no checks ran


async def test_wait_normal_completion_returns_true():
    """Stable price, 1s duration → wait completes normally → True."""
    a = MockClient("a", price=50000)
    result = await wait_with_checks([make_action(a, "bid")], "BTC", make_cfg())
    assert result is True


async def test_wait_stop_loss_exits_early():
    """Price moves past pnl_limit during wait → returns False before duration."""
    a = MockClient("a", price=80000)  # 60% move on long, entry=50000
    result = await wait_with_checks(
        [make_action(a, "bid")], "BTC", make_cfg(trade_duration=[10, 10])
    )
    assert result is False


# MARK: DeltaStrategy trade_cycle


async def test_cycle_opens_opposite_sides():
    """Two accounts always get opposite sides (delta-neutral)."""
    a, b = MockClient("a"), MockClient("b")
    strategy = DeltaStrategy(make_cfg(), [a, b])
    strategy.initial_bal = Decimal("2000")
    await strategy.trade_cycle()

    a_orders = [c for c in a.calls if c == "market_order"]
    b_orders = [c for c in b.calls if c == "market_order"]
    assert len(a_orders) == 1 and len(b_orders) == 1  # each opens exactly once


async def test_cycle_skips_when_no_valid_pair():
    """Extreme balance imbalance → fallback also fails → plan_trades None → no orders.
    a has $0.01, b has $1000 → fallback main_size=9000, rest needs $9000 but has $0.09."""
    a = MockClient("a", balance=0.01)
    b = MockClient("b", balance=1000)
    strategy = DeltaStrategy(make_cfg(), [a, b])
    strategy.initial_bal = Decimal("1000.01")
    await strategy.trade_cycle()
    assert "market_order" not in a.calls
    assert "market_order" not in b.calls


# MARK: DeltaStrategy _loop behavior


async def test_loop_closes_all_on_startup():
    """_loop calls close_all before first trade cycle (clean up previous run)."""
    a, b = MockClient("a"), MockClient("b")
    stop = asyncio.Event()
    strategy = DeltaStrategy(make_cfg(), [a, b], stop_event=stop)

    async def stop_after_cleanup():
        # Wait until close_all has been called (startup), then stop
        for _ in range(50):
            if "cancel_all_orders" in a.calls:
                stop.set()
                return
            await asyncio.sleep(0.05)

    task = asyncio.create_task(strategy._loop())
    await stop_after_cleanup()
    try:
        await asyncio.wait_for(task, timeout=3)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert "cancel_all_orders" in a.calls


async def test_loop_closes_all_on_exception():
    """If trade_cycle raises, _loop calls close_all before re-raising."""
    a, b = MockClient("a"), MockClient("b")
    strategy = DeltaStrategy(make_cfg(), [a, b])

    async def boom():
        raise RuntimeError("exchange down")

    strategy.trade_cycle = boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await strategy._loop()

    assert "cancel_all_orders" in a.calls


# MARK: limit_order_and_wait


async def test_limit_filled_immediately_no_polling():
    """limit_order() returns FILLED order, get_order never called (market-fallback case)."""
    a = MockClient("a")
    # MockClient.limit_order returns FILLED by default
    result = await limit_order_and_wait(a, "BTC", "bid", Decimal("0.002"), Decimal("50000"))
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert "get_order" not in a.calls


async def test_limit_open_get_order_none_raises_fatal(monkeypatch):
    """limit_order() returns OPEN, get_order always None → FatalError (unknown order state)."""
    from lib.http import FatalError

    a = MockClient("a")
    monkeypatch.setattr("strategy.trading.asyncio.sleep", _instant_sleep)

    async def open_limit(s, side, qty, price, reduce_only=False):
        return Order(
            id="ord-l",
            symbol=s,
            side=side,
            size=qty,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
        )

    a.limit_order = open_limit  # type: ignore[method-assign]
    # get_order returns None by default in MockClient

    with pytest.raises(FatalError, match="never appeared"):
        await limit_order_and_wait(a, "BTC", "bid", Decimal("0.002"), Decimal("50000"), timeout=0)


async def test_limit_open_polls_until_filled(monkeypatch):
    """limit_order() returns OPEN, get_order returns FILLED on 3rd call → normal fill path."""
    a = MockClient("a")
    monkeypatch.setattr("strategy.trading.asyncio.sleep", _instant_sleep)
    call_count = 0

    async def open_limit(s, side, qty, price, reduce_only=False):
        return Order(
            id="ord-l",
            symbol=s,
            side=side,
            size=qty,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
        )

    async def eventually_filled(oid):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return Order(
                id=oid,
                symbol="BTC",
                side="bid",
                size=Decimal("0.002"),
                filled=Decimal(0),
                price=Decimal("50000"),
                status=OrderStatus.OPEN,
            )
        return make_order(oid, "BTC", "bid", Decimal("0.002"))

    a.limit_order = open_limit  # type: ignore[method-assign]
    a.get_order = eventually_filled  # type: ignore[method-assign]

    result = await limit_order_and_wait(
        a, "BTC", "bid", Decimal("0.002"), Decimal("50000"), timeout=60
    )
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert call_count >= 3


# MARK: check_min_trade_sizes


async def test_min_sizes_all_ok():
    """All accounts meet minimum → no exception."""
    a, b = MockClient("a"), MockClient("b")
    await check_min_trade_sizes([make_action(a, "bid"), make_action(b, "ask")], "BTC")


async def test_min_sizes_one_fails():
    """One account below minimum → raises naming that account."""
    a, b = MockClient("a"), MockClient("b")
    a._min_trade_usd = Decimal(200)  # action size_usd=100 < 200
    with pytest.raises(RuntimeError, match="a"):
        await check_min_trade_sizes([make_action(a, "bid"), make_action(b, "ask")], "BTC")


async def test_min_sizes_multiple_fail():
    """All accounts below minimum → raises listing all names."""
    a, b = MockClient("a"), MockClient("b")
    a._min_trade_usd = b._min_trade_usd = Decimal(200)
    with pytest.raises(RuntimeError) as exc:
        await check_min_trade_sizes([make_action(a, "bid"), make_action(b, "ask")], "BTC")
    assert "a" in str(exc.value) and "b" in str(exc.value)
