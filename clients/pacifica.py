# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | It's not a bug, it's undocumented behavior
import json
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, Type, cast

import base58
from pydantic import AliasChoices, BaseModel, Field
from solders.keypair import Keypair

from strategy.trading import Order, Position, Side, TradingClient
from utils import helpers as utils
from utils.decorators import bind_log_context, ttl_cache
from utils.http import ApiError, AsyncHttp, HttpMethod
from utils.logger import logger

API_URL = "https://api.pacifica.fi/api/v1"
APP_URL = "https://app.pacifica.fi"
DEFAULT_SLIPPAGE = Decimal("0.5")


class AccountInfo(BaseModel):
    balance: Decimal
    maker_fee: Decimal
    taker_fee: Decimal
    positions_count: int
    orders_count: int
    stop_orders_count: int
    total_margin_used: Decimal


class PointsInfo(BaseModel):
    points: Decimal
    referral_points: Decimal
    volume_7d: Decimal
    last_distribution_points: Decimal
    points_boost: Decimal
    rank: int


class PointsRecord(BaseModel):
    start_window: datetime = Field(..., alias="timestamp")
    total_points: Decimal = Field(..., alias="total_points")


class ApiPosition(BaseModel):
    symbol: str
    side: Side
    amount: Decimal
    entry_price: Decimal


class ApiOrder(BaseModel):
    order_id: int
    symbol: str
    side: Side
    price: Decimal
    initial_amount: Decimal
    filled_amount: Decimal
    cancelled_amount: Decimal
    stop_price: Decimal | None
    order_type: Literal["limit", "market"]
    stop_parent_order_id: int | None
    trigger_price_type: str | None
    reduce_only: bool
    created_at: int
    updated_at: int = Field(validation_alias=AliasChoices("updated_at", "created_at"))
    status: str = Field("open", alias="order_status")


class Trade(BaseModel):
    trade_id: int = Field(..., alias="history_id")
    order_id: int
    symbol: str
    side: Literal["open_long", "open_short", "close_long", "close_short"]
    price: Decimal
    amount: Decimal
    fee: Decimal
    pnl: Decimal
    event_type: str
    created_at: int


class OrderBookItem(BaseModel):
    price: Decimal = Field(..., alias="p")
    amount: Decimal = Field(..., alias="a")
    orders: int = Field(..., alias="n")


def _sign(keypair: Keypair, op_type: str, op_data: dict):
    dat = {
        "type": op_type,
        "data": op_data,
        "timestamp": int(time.time() * 1_000),
        "expiry_window": 5_000,
    }
    msg = json.dumps(dat, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = base58.b58encode(bytes(keypair.sign_message(msg))).decode("ascii")
    return {
        "account": str(keypair.pubkey()),
        "signature": sig,
        "timestamp": dat["timestamp"],
        "expiry_window": dat["expiry_window"],
        **op_data,
    }


@bind_log_context
class Client:
    """Pacifica trading client implementing TradingClient protocol."""

    def __init__(self, name: str, seckey: str, proxy: str | None = None):
        self.keypair = Keypair.from_bytes(base58.b58decode(seckey))
        self.name = name
        self.http = AsyncHttp(
            baseurl=API_URL,
            headers={"Origin": APP_URL, "Referer": f"{APP_URL}/"},
            proxy=proxy,
        )

    async def warmup(self) -> None:
        pass

    async def registered(self) -> bool:
        return True

    async def _call(self, method: HttpMethod, path: str, **kwargs):
        rep = await self.http.request(method, path, **kwargs)
        if not rep.ok and '"success":' not in rep.text:
            raise ApiError(f"Unknown API error: {rep.status_code} {rep.text}")
        res = rep.json()
        if not res["success"]:
            raise ApiError(res["error"])
        return res

    # MARK: Account

    async def account_info(self):
        res = await self._call("GET", f"/account?account={self.keypair.pubkey()}")
        return AccountInfo(**res["data"])

    async def balance(self) -> Decimal:
        return (await self.account_info()).balance

    async def points(self):
        msg = _sign(self.keypair, "get_points", {})
        res = await self._call("POST", "/account/points", json=msg)
        return PointsInfo(**res["data"])

    async def points_history(self):
        msg = _sign(self.keypair, "get_points", {})
        res = await self._call("POST", "/account/points/history", json=msg)
        items = [PointsRecord(**x) for x in res["data"]]
        for x in items:
            x.start_window -= timedelta(seconds=1)
        return items

    async def total_volume(self):
        res = await self._call("GET", f"/portfolio/volume?account={self.keypair.pubkey()}")
        return Decimal(res["data"]["volume_all_time"])

    async def portfolio(self):
        res = await self._call("GET", f"/portfolio?account={self.keypair.pubkey()}&time_range=all")
        res = res["data"][-1]
        return Decimal(res["account_equity"]), Decimal(res["pnl"])

    # MARK: Market data

    @ttl_cache(60)
    async def _info(self):
        res = await self._call("GET", "/info")
        return res["data"]

    async def get_price(self, symbol: str) -> Decimal:
        bids, asks = await self.order_book(symbol)
        return (asks[0].price + bids[0].price) / 2

    async def get_lot_size(self, symbol: str) -> Decimal:
        items = await self._info()
        item = utils.first([x for x in items if x["symbol"] == symbol])
        assert item is not None, f"Unknown symbol: {symbol}"
        return Decimal(item["lot_size"])

    async def get_tick_size(self, symbol: str) -> Decimal:
        items = await self._info()
        item = utils.first([x for x in items if x["symbol"] == symbol])
        assert item is not None, f"Unknown symbol: {symbol}"
        return Decimal(item["tick_size"])

    @ttl_cache(5)
    async def order_book(self, symbol: str, agg_level=1):
        res = await self._call("GET", f"/book?symbol={symbol}&agg_level={agg_level}")
        bids = [OrderBookItem(**x) for x in res["data"]["l"][0]]
        asks = [OrderBookItem(**x) for x in res["data"]["l"][1]]
        return bids, asks

    # MARK: Positions

    async def positions(self) -> list[Position]:
        res = await self._call("GET", f"/positions?account={self.keypair.pubkey()}")
        raw = [ApiPosition(**x) for x in res["data"]]
        return [
            Position(
                id=f"{p.symbol}_{p.side}",
                symbol=p.symbol,
                side=p.side,
                size=abs(p.amount),
                entry_price=p.entry_price,
            )
            for p in raw
            if p.amount != 0
        ]

    async def close_position(self, position: Position) -> bool:
        if position.size == 0:
            return True
        close_side: Side = "ask" if position.side == "bid" else "bid"
        await self.market_order(position.symbol, close_side, position.size, reduce_only=True)
        return True

    async def close_all_positions(self) -> int:
        res = await self._call("GET", f"/positions?account={self.keypair.pubkey()}")
        raw = [ApiPosition(**x) for x in res["data"]]
        closed = 0
        for p in raw:
            if p.amount != 0:
                close_side: Side = "ask" if p.side == "bid" else "bid"
                await self.market_order(p.symbol, close_side, abs(p.amount), reduce_only=True)
                closed += 1
        return closed

    # MARK: Orders

    async def get_order(self, order_id: str) -> Order | None:
        try:
            res = await self._call("GET", f"/orders/history_by_id?order_id={order_id}")
            raw = ApiOrder(**res["data"][0])
            return Order(
                id=str(raw.order_id),
                symbol=raw.symbol,
                side=raw.side,
                size=raw.initial_amount,
                filled=raw.filled_amount,
                price=raw.price,
                status=raw.status,
                reduce_only=raw.reduce_only,
            )
        except Exception:
            return None

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        lot_size = await self.get_lot_size(symbol)
        amount = utils.round_to_tick_size(qty, lot_size)
        logger.debug(f"Market {side} order: {amount} {symbol}")

        dat = {
            "symbol": symbol,
            "amount": str(amount),
            "side": side,
            "slippage_percent": str(DEFAULT_SLIPPAGE),
            "reduce_only": reduce_only,
        }
        msg = _sign(self.keypair, "create_market_order", dat)
        res = await self._call("POST", "/orders/create_market", json=msg)
        order_id = cast(int, res["data"]["order_id"])

        order = await self.get_order(str(order_id))
        if order is None:
            raise ApiError(f"Order {order_id} not found")
        return order

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        tick_size = await self.get_tick_size(symbol)
        lot_size = await self.get_lot_size(symbol)
        price = utils.round_to_tick_size(price, tick_size)
        amount = utils.round_to_tick_size(qty, lot_size)
        logger.debug(f"Limit {side} order: {amount} {symbol} @ {price}")

        dat = {
            "symbol": symbol,
            "amount": str(amount),
            "price": str(price),
            "side": side,
            "tif": "GTC",
            "reduce_only": reduce_only,
        }
        msg = _sign(self.keypair, "create_order", dat)
        res = await self._call("POST", "/orders/create", json=msg)
        order_id = cast(int, res["data"]["order_id"])

        order = await self.get_order(str(order_id))
        if order is None:
            raise ApiError(f"Order {order_id} not found")
        return order

    async def cancel_order(self, order: Order) -> bool:
        try:
            dat = {"order_id": int(order.id), "symbol": order.symbol}
            msg = _sign(self.keypair, "cancel_order", dat)
            await self._call("POST", "/orders/cancel", json=msg)
            return True
        except Exception:
            return False

    async def cancel_all_orders(self) -> int:
        res = await self._call("GET", f"/orders?account={self.keypair.pubkey()}")
        if not res["data"]:
            return 0

        dat = {"all_symbols": True, "exclude_reduce_only": False}
        msg = _sign(self.keypair, "cancel_all_orders", dat)
        res = await self._call("POST", "/orders/cancel_all", json=msg)
        return cast(int, res["data"]["cancelled_count"])

    # MARK: Trades

    async def get_symbols(self) -> list[str]:
        items = await self._info()
        items = sorted(items, key=lambda x: int(x.get("max_leverage", 0)), reverse=True)
        return [x["symbol"] for x in items]

    async def get_leverage(self, symbol: str) -> int | None:
        return None  # no API to fetch current leverage

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        dat = {"symbol": symbol, "leverage": leverage}
        msg = _sign(self.keypair, "update_leverage", dat)
        await self._call("POST", "/account/leverage", json=msg)

    async def trades(self, since: datetime | None = None) -> list[Trade]:
        since_ts = int(since.timestamp() * 1000) if since else None
        has_more, cursor = True, None
        items: dict[int, Trade] = {}

        while has_more:
            url = f"/positions/history?account={self.keypair.pubkey()}&limit=1000"
            url = url + f"&cursor={cursor}" if cursor else url
            res = await self._call("GET", url)
            has_more = res["has_more"]
            cursor = res.get("next_cursor")

            for t in res["data"]:
                t = Trade(**t)
                if since_ts and t.created_at < since_ts:
                    has_more = False
                    break
                items[t.trade_id] = t

        return sorted(items.values(), key=lambda x: x.created_at)


_cls_check: Type[TradingClient] = Client
