# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from strategy.execution import (
    _check_spread_one,
    check_close_safe,
    check_open_safe,
)
from strategy.models import TradeAction


def _mock_client(
    name: str,
    bid: Decimal,
    ask: Decimal,
    pos_size=Decimal(0),
    pos_side="bid",
    entry=Decimal(0),
    price=None,
):
    c = AsyncMock()
    c.name = name
    c.exchange = "test"
    c.get_bbo.return_value = (bid, ask)
    c.get_price.return_value = price or (bid + ask) / 2

    pos = AsyncMock()
    pos.symbol = "BTC"
    pos.size = pos_size
    pos.side = pos_side
    pos.entry_price = entry

    if pos_size > 0:
        c.positions.return_value = [pos]
    else:
        c.positions.return_value = []

    return c


@pytest.mark.asyncio
async def test_spread_one_within_limit():
    c = _mock_client("a", Decimal("99.95"), Decimal("100.05"))
    ok, bps = await _check_spread_one(c, "BTC", max_bps=20)
    assert ok
    assert 9.0 < float(bps) < 11.0  # ~10 bps


@pytest.mark.asyncio
async def test_spread_one_exceeds():
    c = _mock_client("a", Decimal("99.0"), Decimal("101.0"))
    ok, bps = await _check_spread_one(c, "BTC", max_bps=20)
    assert not ok
    assert float(bps) > 100


@pytest.mark.asyncio
async def test_spread_one_disabled():
    c = _mock_client("a", Decimal("99.0"), Decimal("101.0"))
    ok, bps = await _check_spread_one(c, "BTC", max_bps=0)
    assert ok


@pytest.mark.asyncio
async def test_open_safe_passes():
    c1 = _mock_client("a", Decimal("99.99"), Decimal("100.01"))
    c2 = _mock_client("b", Decimal("99.99"), Decimal("100.01"))
    actions = {
        "BTC": [
            TradeAction(client=c1, side="bid", size_usd=Decimal(100), qty=Decimal(1)),
            TradeAction(client=c2, side="ask", size_usd=Decimal(100), qty=Decimal(1)),
        ]
    }
    ok, reason = await check_open_safe(actions, max_spread_bps=10)
    assert ok


@pytest.mark.asyncio
async def test_open_safe_blocks_wide():
    c1 = _mock_client("a", Decimal("99.99"), Decimal("100.01"))
    c2 = _mock_client("b", Decimal("99.50"), Decimal("100.50"))
    actions = {
        "BTC": [
            TradeAction(client=c1, side="bid", size_usd=Decimal(100), qty=Decimal(1)),
            TradeAction(client=c2, side="ask", size_usd=Decimal(100), qty=Decimal(1)),
        ]
    }
    ok, reason = await check_open_safe(actions, max_spread_bps=10)
    assert not ok
    assert "b" in reason


@pytest.mark.asyncio
async def test_close_safe_balanced():
    """delta-neutral perfect: long@100, short@100, mark=100 -> PnL=0"""
    c1 = _mock_client(
        "a",
        Decimal("99.99"),
        Decimal("100.01"),
        pos_size=Decimal(1),
        pos_side="bid",
        entry=Decimal(100),
        price=Decimal(100),
    )
    c2 = _mock_client(
        "b",
        Decimal("99.99"),
        Decimal("100.01"),
        pos_size=Decimal(1),
        pos_side="ask",
        entry=Decimal(100),
        price=Decimal(100),
    )
    actions = {
        "BTC": [
            TradeAction(client=c1, side="bid", size_usd=Decimal(100), qty=Decimal(1)),
            TradeAction(client=c2, side="ask", size_usd=Decimal(100), qty=Decimal(1)),
        ]
    }
    state = await check_close_safe(actions, max_spread_bps=10, max_delta_pnl_pct=0.005)
    assert state.safe
    assert state.delta_pnl_pct == Decimal(0)


@pytest.mark.asyncio
async def test_close_safe_unbalanced():
    """long entry=100 mark=102, short entry=100 mark=101 -> delta !=0"""
    c1 = _mock_client(
        "a",
        Decimal("101.99"),
        Decimal("102.01"),
        pos_size=Decimal(1),
        pos_side="bid",
        entry=Decimal(100),
        price=Decimal(102),
    )
    c2 = _mock_client(
        "b",
        Decimal("100.99"),
        Decimal("101.01"),
        pos_size=Decimal(1),
        pos_side="ask",
        entry=Decimal(100),
        price=Decimal(101),
    )
    actions = {
        "BTC": [
            TradeAction(client=c1, side="bid", size_usd=Decimal(100), qty=Decimal(1)),
            TradeAction(client=c2, side="ask", size_usd=Decimal(100), qty=Decimal(1)),
        ]
    }
    state = await check_close_safe(actions, max_spread_bps=20, max_delta_pnl_pct=0.003)
    assert not state.safe
    assert "delta PnL" in state.reason
