"""Tests for strategy execution and orchestration behavior."""

import asyncio
from decimal import Decimal
from typing import cast

import pytest

from strategy.delta import DeltaStrategy
from strategy.execution import (
    PositionState,
    _position_roi,
    check_min_trade_sizes,
    close_symbol_positions,
    ensure_leverage,
    fill_limit_order,
    hold_positions,
    open_positions,
    positions_within_limits,
)
from strategy.models import (
    Order,
    OrderStatus,
    Position,
    Side,
    StrategyConfig,
    TradeAction,
    TradingClient,
)


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
            "accounts": [{"name": "test", "privkey": "x" * 32}],
            "symbols": ["BTC"],
            "leverage": 10,
            "trade_size_usd": [100, 100],
            "trade_duration": [1, 1],
            "trade_cooldown": [1, 1],
            "trade_heartbeat": 1,
            "position_roi_limit": 0.8,
            "combined_roi_limit": 0.1,
            **kw,
        }
    )


def make_action(client, side: Side, qty: str = "0.002") -> TradeAction:
    return TradeAction(
        client=cast(TradingClient, client), side=side, size_usd=Decimal("100"), qty=Decimal(qty)
    )


def make_symbol_actions(clients: list["MockClient"]) -> dict[str, list[TradeAction]]:
    prime, acc2, acc3 = clients
    return {
        "BTC": [
            TradeAction(cast(TradingClient, prime), "bid", Decimal("25")),
            TradeAction(cast(TradingClient, acc2), "ask", Decimal("10")),
            TradeAction(cast(TradingClient, acc3), "ask", Decimal("15")),
        ],
        "ETH": [
            TradeAction(cast(TradingClient, prime), "ask", Decimal("25")),
            TradeAction(cast(TradingClient, acc2), "bid", Decimal("10")),
            TradeAction(cast(TradingClient, acc3), "bid", Decimal("15")),
        ],
    }


class MockClient(TradingClient):
    """Minimal TradingClient that records calls and returns controllable values."""

    exchange = "mock"

    def __init__(self, name: str, balance: float = 1000, side: Side = "bid", price: float = 50000):
        self._name = name
        self._balance = Decimal(str(balance))
        self._side: Side = side
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

    async def get_price(self, symbol: str):
        self._rec("get_price")
        return self._price

    async def get_bbo(self, symbol: str):
        return Decimal("49999"), Decimal("50001")

    async def get_lot_size(self, symbol: str):
        return Decimal("0.001")

    async def get_tick_size(self, symbol: str):
        return Decimal("1")

    async def get_min_trade_usd(self, symbol: str):
        return self._min_trade_usd

    async def get_leverage(self, symbol: str):
        self._rec("get_leverage")
        return self._leverage

    async def set_leverage(self, symbol: str, leverage: int):
        self._rec("set_leverage")
        self._leverage = leverage

    async def positions(self):
        self._rec("positions")
        return (
            self._positions
            if self._positions is not None
            else [make_position("BTC", self._side, "0.002")]
        )

    async def close_position(self, position: Position):
        self._rec("close_position")
        return True

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False):
        self._rec("market_order")
        return make_order("ord-m", symbol, side, qty)

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ):
        self._rec("limit_order")
        return make_order("ord-l", symbol, side, qty)

    async def cancel_order(self, order: Order):
        self._rec("cancel_order")
        return True

    async def get_order(self, order_id: str):
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


async def test_position_roi_single_position():
    """Single market ROI should return roi, pnl, entry cost, and position size."""
    a = MockClient("a", price=55000)
    a._positions = [make_position("BTC", "bid", "0.002", "50000")]
    assert await _position_roi(a, "BTC") == PositionState(
        roi=Decimal("0.1"), pnl=Decimal("10"), entry_cost=Decimal("100"), size=Decimal("0.002")
    )


async def test_positions_within_limits_detects_size_reduced():
    """Position reduced externally (ADL / partial fill) must trigger emergency close."""
    a, b = MockClient("a"), MockClient("b")
    a._positions = [make_position("BTC", "bid", "0.001", "50000")]  # half of expected
    b._positions = [make_position("BTC", "ask", "0.002", "50000")]

    symbol_actions = {
        "BTC": [make_action(a, "bid", qty="0.002"), make_action(b, "ask", qty="0.002")]
    }

    ok = await positions_within_limits(
        symbol_actions, position_roi_limit=Decimal("0.8"), combined_roi_limit=Decimal("0.1")
    )
    assert ok is False


async def test_positions_within_limits_detects_size_increased():
    """Position grown externally (exchange bug / wrong state) must trigger emergency close."""
    a, b = MockClient("a"), MockClient("b")
    a._positions = [make_position("BTC", "bid", "0.004", "50000")]  # double of expected
    b._positions = [make_position("BTC", "ask", "0.002", "50000")]

    symbol_actions = {
        "BTC": [make_action(a, "bid", qty="0.002"), make_action(b, "ask", qty="0.002")]
    }

    ok = await positions_within_limits(
        symbol_actions, position_roi_limit=Decimal("0.8"), combined_roi_limit=Decimal("0.1")
    )
    assert ok is False


async def test_positions_within_limits_combined_roi():
    """Combined ROI should sum pnl and entry cost across symbols."""
    a = MockClient("prime")
    b = MockClient("acc2")
    a._positions = [make_position("BTC", "bid", "1", "100")]
    b._positions = [make_position("ETH", "bid", "1", "100")]

    async def price_a(symbol):
        return Decimal("120")

    async def price_b(symbol):
        return Decimal("101")

    a.get_price = price_a  # type: ignore[method-assign]
    b.get_price = price_b  # type: ignore[method-assign]

    symbol_actions = {
        "BTC": [TradeAction(a, "bid", Decimal("100"))],
        "ETH": [TradeAction(b, "bid", Decimal("100"))],
    }

    ok = await positions_within_limits(
        symbol_actions,
        position_roi_limit=Decimal("0.8"),
        combined_roi_limit=Decimal("0.1"),
    )

    assert ok is False


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
    """Limit mode: prime uses limit order, rest use market orders."""
    a, b = MockClient("a"), MockClient("b", side="ask")
    filled = make_order("ord-l", "BTC", "bid", Decimal("0.002"))

    async def fake_limit(*args, **kw):
        return filled

    monkeypatch.setattr("strategy.execution._fill_limit_order", fake_limit)
    actions = [make_action(a, "bid"), make_action(b, "ask")]
    await open_positions(actions, "BTC", make_cfg(use_limit=True))
    assert "market_order" not in a.calls  # prime doesn't use market
    assert b.calls.count("market_order") == 1  # rest use market


async def test_open_limit_mode_fails_aborts(monkeypatch):
    """If limit order for prime fails (None), no market orders placed for rest."""
    a, b = MockClient("a"), MockClient("b", side="ask")

    async def fake_limit_fail(*args, **kw):
        return None

    monkeypatch.setattr("strategy.execution._fill_limit_order", fake_limit_fail)
    actions = [make_action(a, "bid"), make_action(b, "ask")]
    await open_positions(actions, "BTC", make_cfg(use_limit=True))
    assert "market_order" not in b.calls  # rest NOT opened
    assert "cancel_all_orders" in a.calls  # cleanup triggered


# MARK: hold_positions


async def test_wait_stop_event_exits_early():
    """Stop event set before wait → returns False immediately."""
    a = MockClient("a")
    stop = asyncio.Event()
    stop.set()
    result = await hold_positions(
        {"BTC": [make_action(a, "bid")]},
        make_cfg(trade_duration=[5, 5]),
        stop,
    )
    assert result is False
    assert "positions" not in a.calls  # no checks ran


async def test_wait_stop_loss_exits_early():
    """Price moves past position_roi_limit → returns False before duration."""
    a = MockClient("a", price=95000)  # 90% move on long, entry=50000
    result = await hold_positions(
        {"BTC": [make_action(a, "bid")]},
        make_cfg(trade_duration=[10, 10]),
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


async def test_cycle_multi_symbol_keeps_double_delta_and_order(monkeypatch):
    # Both symbols must be neutral, and each account must net to zero across symbols.
    accs = [MockClient("prime"), MockClient("acc2"), MockClient("acc3")]
    strategy = DeltaStrategy(make_cfg(symbols=["BTC", "ETH"], symbols_per_trade=2), accs)
    strategy.initial_bal = Decimal("3000")
    symbol_actions = make_symbol_actions(accs)
    checked: list[str] = []
    opened: list[str] = []
    closed: list[tuple[str, bool]] = []

    async def fake_plan(*_args, **_kw):
        return symbol_actions

    async def fake_check(actions, symbol):
        checked.append(symbol)

    async def fake_open(self, symbol, actions):
        opened.append(symbol)

    async def fake_wait(*_args, **_kw):
        return True

    async def fake_close(accs, symbol, cfg, use_limit=False):
        closed.append((symbol, use_limit))

    monkeypatch.setattr("strategy.delta.random.sample", lambda seq, n: list(seq)[:n])
    monkeypatch.setattr("strategy.delta.plan_symbol_actions", fake_plan)
    monkeypatch.setattr("strategy.delta.check_min_trade_sizes", fake_check)
    monkeypatch.setattr("strategy.delta.hold_positions", fake_wait)
    monkeypatch.setattr(DeltaStrategy, "open_symbol_positions", fake_open)
    monkeypatch.setattr("strategy.delta.close_symbol_positions", fake_close)

    await strategy.trade_cycle()

    assert checked == ["BTC", "ETH"]
    assert opened == ["BTC", "ETH"]
    assert closed == [("BTC", False), ("ETH", False)]

    for actions in symbol_actions.values():
        bids = sum(x.size_usd for x in actions if x.side == "bid")
        asks = sum(x.size_usd for x in actions if x.side == "ask")
        assert bids == asks

    totals = {acc.name: {"bid": Decimal(0), "ask": Decimal(0)} for acc in accs}
    for actions in symbol_actions.values():
        for action in actions:
            totals[action.client.name][action.side] += action.size_usd

    for acc in accs:
        assert totals[acc.name]["bid"] == totals[acc.name]["ask"]


async def test_cycle_aborts_before_open_when_any_symbol_min_size_fails(monkeypatch):
    # Min-size validation must finish for all symbols before any open starts.
    accs = [MockClient("prime"), MockClient("acc2"), MockClient("acc3")]
    strategy = DeltaStrategy(make_cfg(symbols=["BTC", "ETH"], symbols_per_trade=2), accs)
    strategy.initial_bal = Decimal("3000")
    opened: list[str] = []

    async def fake_plan(*_args, **_kw):
        return make_symbol_actions(accs)

    async def fake_check(actions, symbol):
        if symbol == "ETH":
            raise RuntimeError("min size fail")

    async def fake_open(self, symbol, actions):
        opened.append(symbol)

    monkeypatch.setattr("strategy.delta.random.sample", lambda seq, n: list(seq)[:n])
    monkeypatch.setattr("strategy.delta.plan_symbol_actions", fake_plan)
    monkeypatch.setattr("strategy.delta.check_min_trade_sizes", fake_check)
    monkeypatch.setattr(DeltaStrategy, "open_symbol_positions", fake_open)

    with pytest.raises(RuntimeError, match="min size fail"):
        await strategy.trade_cycle()

    assert opened == []


async def test_close_symbol_positions_passes_single_symbol_and_order(monkeypatch):
    # Symbol close helper must preserve account order for one symbol.
    accs = [MockClient("prime"), MockClient("acc2"), MockClient("acc3")]
    seen: list[tuple[str, list[str], bool]] = []

    async def fake_positions_main():
        return [make_position("ETH", "ask", "0.002")]

    async def fake_positions_acc2():
        return [make_position("ETH", "bid", "0.002")]

    async def fake_positions_acc3():
        return [make_position("ETH", "bid", "0.002")]

    async def fake_fill(*args, **kwargs):
        return make_order("ord-l", "ETH", "bid", Decimal("0.002"))

    async def fake_close_position(position):
        seen.append(("ETH", [position.symbol], True))
        return True

    accs[0].positions = fake_positions_main  # type: ignore[method-assign]
    accs[1].positions = fake_positions_acc2  # type: ignore[method-assign]
    accs[2].positions = fake_positions_acc3  # type: ignore[method-assign]
    accs[1].close_position = fake_close_position  # type: ignore[method-assign]
    accs[2].close_position = fake_close_position  # type: ignore[method-assign]
    monkeypatch.setattr("strategy.execution._fill_limit_order", fake_fill)

    await close_symbol_positions(cast(list[TradingClient], accs), "ETH", make_cfg(), use_limit=True)

    assert seen == [("ETH", ["ETH"], True), ("ETH", ["ETH"], True)]


# MARK: DeltaStrategy run behavior


async def test_loop_closes_all_on_startup():
    """run() calls close_all before first trade cycle (clean up previous run)."""
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

    task = asyncio.create_task(strategy.run())
    await stop_after_cleanup()
    try:
        await asyncio.wait_for(task, timeout=3)
    except asyncio.CancelledError, asyncio.TimeoutError:
        pass

    assert "cancel_all_orders" in a.calls


async def test_loop_closes_all_on_exception():
    """If trade_cycle raises, run() calls close_all and retries until MAX_FAILURES."""
    a, b = MockClient("a"), MockClient("b")
    strategy = DeltaStrategy(make_cfg(), [a, b])
    strategy._wait = lambda _sec: asyncio.sleep(0)  # type: ignore[method-assign]

    async def boom():
        raise RuntimeError("exchange down")

    strategy.trade_cycle = boom  # type: ignore[method-assign]

    await strategy.run()  # returns cleanly after MAX_FAILURES (no raise)

    assert "cancel_all_orders" in a.calls


# MARK: fill_limit_order


async def test_limit_filled_immediately_no_polling():
    """limit_order() returns FILLED order, get_order never called (market-fallback case)."""
    a = MockClient("a")
    # MockClient.limit_order returns FILLED by default
    result = await fill_limit_order(a, "BTC", "bid", Decimal("0.002"), Decimal("50000"))
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert "get_order" not in a.calls


async def test_limit_open_get_order_none_raises_fatal(monkeypatch):
    """limit_order() returns OPEN, get_order always None → FatalError (unknown order state)."""
    from lib.http import FatalError

    a = MockClient("a")
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)

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
        await fill_limit_order(a, "BTC", "bid", Decimal("0.002"), Decimal("50000"), timeout=0)


async def test_limit_open_polls_until_filled(monkeypatch):
    """limit_order() returns OPEN, get_order returns FILLED on 3rd call → normal fill path."""
    a = MockClient("a")
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
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

    result = await fill_limit_order(a, "BTC", "bid", Decimal("0.002"), Decimal("50000"), timeout=60)
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert call_count >= 3


async def test_limit_canceled_by_exchange_raises(monkeypatch):
    """Exchange-canceled order → RuntimeError regardless of use_market_fallback."""
    a = MockClient("a")
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    qty = Decimal("0.002")

    async def open_limit(s, side, q, price, reduce_only=False):
        return Order(
            id="ord-l",
            symbol=s,
            side=side,
            size=q,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
        )

    async def get_canceled(oid):
        return Order(
            id=oid,
            symbol="BTC",
            side="bid",
            size=qty,
            filled=Decimal(0),
            price=Decimal("50000"),
            status=OrderStatus.CANCELED,
        )

    a.limit_order = open_limit  # type: ignore[method-assign]
    a.get_order = get_canceled  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="canceled by exchange"):
        await fill_limit_order(a, "BTC", "bid", qty, Decimal("50000"), use_market_fallback=True)
    assert "market_order" not in a.calls


async def test_limit_timeout_uses_market_fallback(monkeypatch):
    """Order still OPEN after timeout → canceled then filled via market."""
    a = MockClient("a")
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    qty = Decimal("0.002")

    async def open_limit(s, side, q, price, reduce_only=False):
        return Order(
            id="ord-l",
            symbol=s,
            side=side,
            size=q,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
        )

    async def still_open(oid):
        return Order(
            id=oid,
            symbol="BTC",
            side="bid",
            size=qty,
            filled=Decimal(0),
            price=Decimal("50000"),
            status=OrderStatus.OPEN,
        )

    a.limit_order = open_limit  # type: ignore[method-assign]
    a.get_order = still_open  # type: ignore[method-assign]

    result = await fill_limit_order(
        a, "BTC", "bid", qty, Decimal("50000"), timeout=0, use_market_fallback=True
    )
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert "cancel_order" in a.calls
    assert "market_order" in a.calls


async def test_limit_timeout_no_fallback_raises(monkeypatch):
    """Timeout with use_market_fallback=False → RuntimeError with reason."""
    a = MockClient("a")
    monkeypatch.setattr("strategy.execution.asyncio.sleep", _instant_sleep)
    qty = Decimal("0.002")

    async def open_limit(s, side, q, price, reduce_only=False):
        return Order(
            id="ord-l",
            symbol=s,
            side=side,
            size=q,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
        )

    async def still_open(oid):
        return Order(
            id=oid,
            symbol="BTC",
            side="bid",
            size=qty,
            filled=Decimal(0),
            price=Decimal("50000"),
            status=OrderStatus.OPEN,
        )

    a.limit_order = open_limit  # type: ignore[method-assign]
    a.get_order = still_open  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="timed out"):
        await fill_limit_order(
            a, "BTC", "bid", qty, Decimal("50000"), timeout=0, use_market_fallback=False
        )
    assert "cancel_order" in a.calls
    assert "market_order" not in a.calls


# MARK: check_min_trade_sizes


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
