# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | If it compiles, ship it
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Type

from eth_account import Account
from eth_account.messages import encode_defunct
from pydantic import AliasPath, BaseModel, Field

from strategy.trading import Order, Position, Side, TradingClient
from utils import helpers as utils
from utils.decorators import bind_log_context, retry
from utils.http import ApiError, AsyncHttp, HttpMethod
from utils.logger import logger

API_URL = "https://omni.variational.io/api"
APP_URL = "https://omni.variational.io"


class IndicativeQuote(BaseModel):
    quote_id: str
    mark_price: Decimal
    index_price: Decimal
    bid: Decimal
    ask: Decimal
    qty: Decimal
    qty_tick: Decimal = Field(validation_alias=AliasPath("qty_limits", "bid", "min_qty_tick"))


class PointsInfo(BaseModel):
    total_points: Decimal
    rank: int | None = None


class PointsRecord(BaseModel):
    start_window: datetime
    total_points: Decimal


class ApiPosition(BaseModel):
    symbol: str = Field(validation_alias=AliasPath("position_info", "instrument", "underlying"))
    qty: Decimal = Field(validation_alias=AliasPath("position_info", "qty"))
    entry_price: Decimal = Field(validation_alias=AliasPath("position_info", "avg_entry_price"))


class ApiOrder(BaseModel):
    id: str = Field(validation_alias="rfq_id")
    created_at: datetime
    market: str = Field(validation_alias=AliasPath("instrument", "underlying"))
    qty: Decimal
    side: str
    status: str
    is_reduce_only: bool
    limit_price: Decimal | None


@bind_log_context
class Client:
    """Omni trading client implementing TradingClient protocol."""

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        self.account = Account.from_key(privkey)
        self.address = self.account.address
        self.name = name
        self.http = AsyncHttp(
            baseurl=API_URL,
            headers={"Origin": APP_URL, "Referer": f"{APP_URL}/"},
            proxy=proxy,
            cookies_file=f".cache/omni_{utils.short_addr(self.address)}.pkl",
        )

    @retry(max_attempts=9, delay=2.0)
    async def warmup(self) -> None:
        rep = await self.http.request("GET", "https://omni.variational.io/")
        assert rep.ok, f"Warmup failed: {rep.status_code} {rep.text[:200]}"

    @retry(max_attempts=3, delay=1.0)
    async def registered(self) -> bool:
        rep = await self.http.request("GET", f"/auth/company/{self.address}")
        rep.raise_for_status()
        res = rep.json()
        return res["company"] is not None and res["settlement_pool"] is not None

    @retry(max_attempts=3, delay=1.0)
    async def _ensure_auth(self):
        if "vr-token" in self.http.session.cookies:
            return True
        pld = {"address": self.address}
        rep = await self.http.request("POST", f"{API_URL}/auth/generate_signing_data", json=pld)
        if not rep.text.startswith("omni.variational.io wants you to"):
            raise ApiError(f"Unexpected signing data: {rep.text}")

        msg = encode_defunct(text=rep.text)
        sig = self.account.sign_message(msg).signature.hex().replace("0x", "")

        pld = {"address": self.address, "signed_message": sig}
        rep = await self.http.request("POST", f"{API_URL}/auth/login", json=pld)
        if not rep.ok or "vr-token" not in self.http.session.cookies:
            raise ApiError(f"Login failed: {rep.status_code} {rep.text}")
        return True

    async def _call(self, method: HttpMethod, path: str, **kwargs):
        await self._ensure_auth()
        rep = await self.http.request(method, path, **kwargs)
        logger.trace(f">> {method} {path} response: {rep.status_code}")
        if not rep.ok:
            raise ApiError(f"API error: {rep.status_code} {rep.text}")
        return rep.json()

    # MARK: Account

    async def balance(self) -> Decimal:
        res = await self._call("GET", "/portfolio?compute_margin=true")
        return Decimal(res["balance"])

    async def points(self) -> PointsInfo:
        res = await self._call("GET", "/points/summary")
        return PointsInfo(**res)

    async def points_history(self) -> list[PointsRecord]:
        records = await self._call("GET", "/points/history", params={"limit": 20})
        return [PointsRecord(**r) for r in records if Decimal(r["total_points"]) > 0]

    async def total_volume(self) -> Decimal:
        res = await self._call("GET", "/referrals/summary")
        if "trade_volume" in res:
            return Decimal(res["trade_volume"]["current"])
        elif "own_volume" in res:
            return Decimal(res["own_volume"]["total"])
        return Decimal(0)

    async def pnl(self) -> Decimal:
        params = {"limit": 20, "offset": 0, "period": "total", "ranking": "pnl"}
        res = await self._call("GET", "/leaderboard", params=params)
        data = res.get("result", {}).get("self", {})
        return Decimal(data.get("pnl", 0))

    async def get_symbols(self) -> list[str]:
        # Returns available perpetual future underlyings sorted by some exchange-defined order.
        # Endpoint verified against https://omni.variational.io/api/instruments
        res = await self._call("GET", "/instruments")
        items = [x for x in res if x.get("instrument_type") == "perpetual_future"]
        return [x["underlying"] for x in items]

    async def get_leverage(self, symbol: str) -> int:
        res = await self._call("POST", "/settlement_pools/leverage", json={"assets": [symbol]})
        return int(res[symbol]["current"])

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        assert 1 <= leverage <= 50, "Leverage must be between 1 and 50"
        dat = {"leverage": leverage, "asset": symbol}
        res = await self._call("POST", "/settlement_pools/set_leverage", json=dat)
        assert int(res["current"]) == leverage

    async def fetch_history(self, endpoint: str, since: datetime | None = None) -> list[Any]:
        since = since or datetime(2026, 1, 1, tzinfo=UTC)
        until = datetime.now(tz=UTC).replace(hour=23, minute=59, second=59, microsecond=999000)
        pld = {
            "order_by": "created_at",
            "order": "desc",
            "limit": 20,
            "offset": 0,
            "created_at_gte": since.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "created_at_lte": until.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        items = []
        while True:
            res = await self._call("GET", endpoint, params=pld)
            items.extend(res.get("result", []))
            pld["offset"] += pld["limit"]
            if not res.get("pagination", {}).get("next_page"):
                break
        return items

    # MARK: Market data

    async def _quote(self, asset: str, qty: Decimal | int | float) -> IndicativeQuote:
        pld = {
            "instrument": {
                "underlying": asset,
                "funding_interval_s": 3600,
                "settlement_asset": "USDC",
                "instrument_type": "perpetual_future",
            },
            "qty": str(qty),
        }
        res = await self._call("POST", "/quotes/indicative", json=pld)
        return IndicativeQuote(**res)

    async def get_price(self, symbol: str) -> Decimal:
        return (await self._quote(symbol, 1)).mark_price

    async def get_lot_size(self, symbol: str) -> Decimal:
        return (await self._quote(symbol, 1)).qty_tick

    async def get_tick_size(self, symbol: str) -> Decimal:
        return Decimal("0.01")

    # MARK: Positions

    async def positions(self) -> list[Position]:
        items = await self._call("GET", "/positions")
        raw = [ApiPosition(**x) for x in items]
        return [
            Position(
                id=p.symbol,
                symbol=p.symbol,
                side="bid" if p.qty > 0 else "ask",
                size=abs(p.qty),
                entry_price=p.entry_price,
            )
            for p in raw
            if p.qty != 0
        ]

    async def close_position(self, position: Position) -> bool:
        if position.size == 0:
            return True
        signed_qty = -position.size if position.side == "bid" else position.size
        quote = await self._quote(position.symbol, abs(signed_qty))
        pld = {
            "quote_id": quote.quote_id,
            "side": "sell" if signed_qty < 0 else "buy",
            "max_slippage": 0.001,
            "is_reduce_only": True,
        }
        await self._call("POST", "/quotes/accept", json=pld)
        return True

    async def close_all_positions(self) -> int:
        items = await self._call("GET", "/positions")
        raw = [ApiPosition(**x) for x in items]
        count = 0
        for p in raw:
            if p.qty != 0:
                quote = await self._quote(p.symbol, abs(p.qty))
                pld = {
                    "quote_id": quote.quote_id,
                    "side": "sell" if p.qty > 0 else "buy",
                    "max_slippage": 0.001,
                    "is_reduce_only": True,
                }
                await self._call("POST", "/quotes/accept", json=pld)
                count += 1
        return count

    # MARK: Orders

    async def get_order(self, order_id: str) -> Order | None:
        return None  # Omni doesn't have order lookup

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        signed_qty = qty if side == "bid" else -qty
        quote = await self._quote(symbol, abs(signed_qty))
        logger.debug(f"Market {'buy' if side == 'bid' else 'sell'} order: {qty} {symbol}")
        pld = {
            "quote_id": quote.quote_id,
            "side": "buy" if side == "bid" else "sell",
            "max_slippage": 0.001 if reduce_only else 0.005,
            "is_reduce_only": reduce_only,
        }
        url = "/quotes/accept" if reduce_only else "/orders/new/market"
        res = await self._call("POST", url, json=pld)
        order_id = res.get("rfq_id", res.get("order_id", ""))
        return Order(
            id=str(order_id),
            symbol=symbol,
            side=side,
            size=qty,
            filled=qty,
            price=None,
            status="filled",
            reduce_only=reduce_only,
        )

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        # TODO: Limit orders not implemented for current strategy, since Omni have zero-fees
        return await self.market_order(symbol, side, qty, reduce_only)

    async def cancel_order(self, order: Order) -> bool:
        try:
            res = await self._call("POST", "/orders/cancel", json={"rfq_id": order.id})
            logger.debug(f"Cancel order response: {res}")
            return True
        except Exception:
            return False

    async def cancel_all_orders(self) -> int:
        pld = {"status": "pending", "order_by": "created_at", "order": "desc"}
        pld = {**pld, "limit": 20, "offset": 0}

        items = []
        while True:
            res = await self._call("GET", "/orders/v2", params=pld)
            items.extend(res.get("result", []))
            pld["offset"] += pld["limit"]
            if not res.get("pagination", {}).get("next_page"):
                break

        for x in items:
            await self._call("POST", "/orders/cancel", json={"rfq_id": x["rfq_id"]})

        return len(items)


_cls_check: Type[TradingClient] = Client
