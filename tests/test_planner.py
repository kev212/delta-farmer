from decimal import Decimal
from typing import cast

import pytest

from strategy.execution import TradeAction
from strategy.planner import calc_symbol_sizes, plan_symbol_actions
from strategy.trading import Side, TradingClient


class DummyClient:
    def __init__(self, name: str):
        self._name = name
        self._balance = Decimal("1000")

    @property
    def name(self) -> str:
        return self._name

    async def balance(self) -> Decimal:
        return self._balance


def _sum_side(actions: list[TradeAction], side: Side) -> Decimal:
    return sum((x.size_usd for x in actions if x.side == side), Decimal(0))


def test_calc_symbol_sizes_contains_direction():
    sides: list[Side] = ["bid", "ask"]
    for side in sides:
        sizes = calc_symbol_sizes(Decimal("100"), ["BTC", "ETH", "SOL"], side)
        opposite = "ask" if side == "bid" else "bid"
        assert sizes == {
            "BTC": (Decimal("50.000"), side),
            "ETH": (Decimal("25.000"), opposite),
            "SOL": (Decimal("25.000"), opposite),
        }


def test_calc_symbol_sizes_supported_counts():
    expected = [
        {"BTC": ("100", "bid")},
        {"BTC": ("50", "bid"), "ETH": ("50", "ask")},
        {"BTC": ("50", "bid"), "ETH": ("25", "ask"), "SOL": ("25", "ask")},
        {"BTC": ("25", "bid"), "ETH": ("25", "bid"), "SOL": ("25", "ask"), "XRP": ("25", "ask")},
    ]

    for i in range(len(expected)):
        symbols = list(expected[i].keys())
        expect = {m: (Decimal(s), d) for m, (s, d) in expected[i].items()}
        assert calc_symbol_sizes(Decimal("100"), symbols, "bid") == expect


def test_calc_symbol_sizes_rejects_more_than_five_symbols():
    symbols = ["BTC", "ETH", "SOL", "XRP", "DOGE"]

    with pytest.raises(ValueError, match="up to 4 symbols"):
        calc_symbol_sizes(Decimal("100"), symbols, "bid")


async def test_plan_symbol_actions_keeps_symbol_and_account_delta_neutral(monkeypatch):
    accounts = [DummyClient("main"), DummyClient("acc2"), DummyClient("acc3")]

    def fake_find_safe_pair(_balances, _size_usd, _leverage):
        return [("main", Decimal("50")), ("acc2", Decimal("20")), ("acc3", Decimal("30"))]

    monkeypatch.setattr("strategy.planner.find_safe_pair", fake_find_safe_pair)
    monkeypatch.setattr("strategy.planner.random.choice", lambda _: "bid")

    plan = await plan_symbol_actions(
        accounts=cast(list[TradingClient], accounts),
        symbols=["BTC", "ETH", "SOL"],
        total_size_usd=Decimal("100"),
        leverage=10,
    )

    assert plan is not None

    for actions in plan.values():
        assert _sum_side(actions, "bid") == _sum_side(actions, "ask")

    totals: dict[str, dict[str, Decimal]] = {
        "main": {"bid": Decimal(0), "ask": Decimal(0)},
        "acc2": {"bid": Decimal(0), "ask": Decimal(0)},
        "acc3": {"bid": Decimal(0), "ask": Decimal(0)},
    }
    for actions in plan.values():
        for action in actions:
            totals[action.client.name][action.side] += action.size_usd

    assert totals["main"]["bid"] == totals["main"]["ask"] == Decimal("25.000")
    assert totals["acc2"]["bid"] == totals["acc2"]["ask"] == Decimal("10.000")
    assert totals["acc3"]["bid"] == totals["acc3"]["ask"] == Decimal("15.000")


async def test_plan_symbol_actions_returns_none_when_pair_not_found(monkeypatch):
    accounts = [DummyClient("a"), DummyClient("b")]
    monkeypatch.setattr("strategy.planner.find_safe_pair", lambda *_: None)

    plan = await plan_symbol_actions(
        accounts=cast(list[TradingClient], accounts),
        symbols=["BTC", "ETH"],
        total_size_usd=Decimal("100"),
        leverage=10,
    )

    assert plan is None


async def test_plan_symbol_actions_uses_actual_pair_total(monkeypatch):
    accounts = [DummyClient("main"), DummyClient("acc2"), DummyClient("acc3")]

    def fake_find_safe_pair(_balances, _size_usd, _leverage):
        return [("main", Decimal("40")), ("acc2", Decimal("10")), ("acc3", Decimal("30"))]

    monkeypatch.setattr("strategy.planner.find_safe_pair", fake_find_safe_pair)
    monkeypatch.setattr("strategy.planner.random.choice", lambda _: "bid")

    plan = await plan_symbol_actions(
        accounts=cast(list[TradingClient], accounts),
        symbols=["BTC", "ETH"],
        total_size_usd=Decimal("100"),
        leverage=10,
    )

    assert plan is not None
    # Fallback sizing from find_safe_pair overrides the requested total.
    assert sum(x.size_usd for x in plan["BTC"]) == Decimal("40.000")
    assert sum(x.size_usd for x in plan["ETH"]) == Decimal("40.000")
