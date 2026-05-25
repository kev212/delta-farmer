# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from lib.models import TgConfig
from lib.telegram import _state as tg_state
from lib.telegram_commands import (
    _auth,
    _cmd_balance,
    _cmd_info,
    _cmd_positions,
    _cmd_spread,
    _cmd_uptime,
    handle_message,
)


def _mock_acc(
    name: str, balance=Decimal(100), address="0x1234567890abcdef1234567890abcdef12345678"
):
    acc = AsyncMock()
    acc.name = name
    acc.address = address
    acc.balance.return_value = balance
    return acc


def _mock_pos_acc(name: str, positions=None, balance=Decimal(100)):
    acc = AsyncMock()
    acc.name = name
    acc.balance.return_value = balance
    acc.positions.return_value = positions or []
    return acc


def _setup_state(accounts=None, commands_enabled=True, chat_id="12345"):
    tg_state.cfg = TgConfig(token="test:token", chat_id=chat_id, commands_enabled=commands_enabled)
    tg_state.accounts = accounts or []


class TestAuth:
    def test_auth_same_chat_id(self):
        _setup_state(chat_id="12345")
        assert _auth("12345") is True

    def test_auth_wrong_chat_id(self):
        _setup_state(chat_id="12345")
        assert _auth("99999") is False


class TestBalance:
    @pytest.mark.asyncio
    async def test_balance_no_accounts(self):
        _setup_state([])
        result = await _cmd_balance()
        assert "No accounts" in result

    @pytest.mark.asyncio
    async def test_balance_single(self):
        acc = _mock_acc("omni-a", Decimal("50.42"))
        _setup_state([acc])
        result = await _cmd_balance()
        assert "omni-a" in result
        assert "50.42" in result


class TestInfo:
    @pytest.mark.asyncio
    async def test_info_no_accounts(self):
        _setup_state([])
        result = await _cmd_info()
        assert "No accounts" in result

    @pytest.mark.asyncio
    async def test_info_shows_accounts(self):
        a1 = _mock_acc("omni-a", Decimal("50.40"))
        a2 = _mock_acc("omni-b", Decimal("50.20"))
        _setup_state([a1, a2])
        result = await _cmd_info()
        assert "omni-a" in result
        assert "omni-b" in result
        assert "100.60" in result  # total


class TestPositions:
    @pytest.mark.asyncio
    async def test_positions_empty(self):
        acc = _mock_pos_acc("omni-a", [])
        _setup_state([acc])
        result = await _cmd_positions()
        assert "No open positions" in result

    @pytest.mark.asyncio
    async def test_positions_listed(self):
        from strategy import Position

        pos = Position(
            id="1", symbol="BTC", side="bid", size=Decimal("0.001"), entry_price=Decimal("77000")
        )
        acc = _mock_pos_acc("omni-a", [pos])
        _setup_state([acc])
        result = await _cmd_positions()
        assert "omni-a" in result
        assert "BTC" in result
        assert "0.0010" in result


class TestSpread:
    @pytest.mark.asyncio
    async def test_spread_no_symbol(self):
        acc = _mock_acc("omni-a")
        _setup_state([acc])
        result = await _cmd_spread("")
        assert "Usage" in result

    @pytest.mark.asyncio
    async def test_spread_result(self):
        acc = _mock_acc("omni-a")

        # Mock BBO
        async def fake_bbo(sym):
            return Decimal("77100"), Decimal("77105")

        acc.get_bbo = fake_bbo
        _setup_state([acc])
        result = await _cmd_spread("BTC")
        assert "77100" in result
        assert "77105" in result
        assert "BTC" in result


class TestUptime:
    @pytest.mark.asyncio
    async def test_uptime_returns_string(self):
        _setup_state()
        result = await _cmd_uptime()
        assert isinstance(result, str)
        assert "Uptime" in result


class TestDispatch:
    @pytest.mark.asyncio
    async def test_unauthorized_chat(self):
        _setup_state(chat_id="12345")
        acc = _mock_acc("omni-a")
        tg_state.accounts = [acc]
        result = await handle_message("99999", "/balance")
        assert result is None  # unauthorized = silent ignore

    @pytest.mark.asyncio
    async def test_unknown_command_silent(self):
        _setup_state()
        result = await handle_message("12345", "/impossible_command_xyz")
        assert result is None  # unknown cmd = silent ignore

    @pytest.mark.asyncio
    async def test_balance_dispatch(self):
        _setup_state(chat_id="12345")
        acc = _mock_acc("omni-a", Decimal("50.42"))
        tg_state.accounts = [acc]
        result = await handle_message("12345", "/balance")
        assert result is not None
        assert "50.42" in result
