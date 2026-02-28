# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import time
from decimal import Decimal
from typing import Any, Literal, Type

from eth_account import Account
from eth_account.messages import encode_typed_data
from pydantic import BaseModel, Field

from strategy.trading import Order, OrderStatus, Position, Side, TradingClient
from utils import helpers as utils
from utils.decorators import bind_log_context, retry
from utils.http import ApiError, AsyncHttp, FatalError, HttpMethod

API_URL = "https://api.ethereal.trade/v1"
APP_URL = "https://app.ethereal.trade"

NativeSide = Literal["buy", "sell"]


# https://docs.ethereal.trade/developer-guides/trading-api/order-placement#lifecycle
def to_domain_status(s: str) -> OrderStatus:
    s = s.lower()
    if s in ("new", "pending", "open", "partially_filled"):
        return OrderStatus.OPEN
    if s == "filled":
        return OrderStatus.FILLED
    if s in ("canceled", "cancelled", "expired", "rejected"):
        return OrderStatus.CANCELED
    raise ValueError(f"Unknown order status: {s}")


def _to_native(side: Side) -> NativeSide:
    return "buy" if side == "bid" else "sell"


def _from_native(native: NativeSide) -> Side:
    return "bid" if native == "buy" else "ask"


class Subaccount(BaseModel):
    id: str
    name: str
    account: str


class BalanceItem(BaseModel):
    subaccountId: str
    tokenName: str
    amount: Decimal
    available: Decimal
    totalUsed: Decimal


class Product(BaseModel):
    id: str
    ticker: str
    base_token: str = Field(alias="baseTokenName")
    onchain_id: int = Field(alias="onchainId")
    lot_size: Decimal = Field(alias="lotSize")
    tick_size: Decimal = Field(alias="tickSize")
    min_quantity: Decimal = Field(alias="minQuantity")
    max_leverage: int = Field(alias="maxLeverage")


class ApiPosition(BaseModel):
    id: str
    product_id: str = Field(alias="productId")
    size: Decimal
    side: NativeSide
    cost: Decimal
    realized_pnl: Decimal = Field(alias="realizedPnl")
    fees_usd: Decimal = Field(default=Decimal(0), alias="feesAccruedUsd")
    funding_usd: Decimal = Field(default=Decimal(0), alias="fundingAccruedUsd")
    total_inc: Decimal = Field(default=Decimal(0), alias="totalIncreaseNotional")
    total_dec: Decimal = Field(default=Decimal(0), alias="totalDecreaseNotional")
    created_at: int = Field(default=0, alias="createdAt")
    symbol: str = ""


class ApiOrder(BaseModel):
    id: str
    product_id: str = Field(alias="productId")
    side: NativeSide
    order_type: str = Field(alias="type")
    price: Decimal | None = None
    quantity: Decimal
    available_quantity: Decimal = Field(alias="availableQuantity")
    filled_quantity: Decimal = Field(default=Decimal(0), alias="filledQuantity")
    status: str
    reduce_only: bool = Field(default=False, alias="reduceOnly")
    created_at: int = Field(alias="createdAt")
    symbol: str = ""


class PointsInfo(BaseModel):
    total_points: Decimal = Field(alias="totalPoints")
    referral_points: Decimal = Field(alias="referralPoints")
    rank: int
    tier: int


class PointsRecord(BaseModel):
    id: str
    season: int
    epoch: int
    points: Decimal
    referral_points: Decimal = Field(alias="referralPoints")
    started_at: int = Field(alias="startedAt")
    ended_at: int = Field(alias="endedAt")


class Fill(BaseModel):
    id: str
    order_id: str = Field(alias="orderId")
    product_id: str = Field(alias="productId")
    side: NativeSide
    price: Decimal
    filled: Decimal
    fee_usd: Decimal = Field(alias="feeUsd")
    is_maker: bool = Field(alias="isMaker")
    order_type: str = Field(alias="type")
    reduce_only: bool = Field(alias="reduceOnly")
    created_at: int = Field(alias="createdAt")
    symbol: str = ""


def _encode_subaccount_name(name: str) -> str:
    if name.startswith("0x") and len(name) == 66:
        return name.lower()
    raw = name.encode("ascii")
    if len(raw) > 32:
        raise ValueError("subaccount_name must be <= 32 ascii bytes")
    return "0x" + raw.hex().ljust(64, "0")


def _parse_signature_type(value: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        ftype, fname = part.rsplit(" ", 1)
        out.append({"name": fname, "type": ftype})
    return out


@bind_log_context
class Client:
    """Ethereal trading client implementing TradingClient protocol."""

    def __init__(
        self,
        name: str,
        privkey: str,
        proxy: str | None = None,
    ):
        self.name = name
        self.owner = Account.from_key(privkey)
        self.address = self.owner.address
        self.http = AsyncHttp(
            baseurl=API_URL,
            headers={"Origin": APP_URL, "Referer": f"{APP_URL}/"},
            proxy=proxy,
            cookies_file=f".cache/ethereal_{utils.short_addr(self.address)}.pkl",
        )
        self._domain: dict[str, Any] | None = None
        self._types: dict[str, list[dict[str, str]]] | None = None
        self._subaccount: Subaccount | None = None
        self._products_cache: dict[str, Product] | None = None

    @retry(max_attempts=9, delay=2.0)
    async def warmup(self) -> None:
        rep = await self.http.request("GET", APP_URL)
        assert rep.ok, f"Warmup failed: {rep.status_code} {rep.text[:200]}"

    async def _call(self, method: HttpMethod, path: str, **kwargs):
        rep = await self.http.request(method, path, **kwargs)
        if not rep.ok:
            raise ApiError(f"API error: {rep.status_code} {rep.text}")
        return rep.json()

    async def _ensure_auth(self):
        # Load RPC config
        if self._domain is None or self._types is None:
            res = await self._call("GET", "/rpc/config")
            self._domain = res["domain"]
            types: dict[str, list[dict[str, str]]] = {}
            for n, v in res["signatureTypes"].items():
                types[n] = _parse_signature_type(v)
            types["EIP712Domain"] = [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ]
            self._types = types

        # Resolve subaccount
        if self._subaccount is None:
            target_name = _encode_subaccount_name("primary")
            res = await self._call("GET", "/subaccount", params={"sender": self.address})
            items = [Subaccount(**x) for x in res.get("data", [])]
            if not items:
                raise ApiError(f"No subaccounts found for {self.address}")
            self._subaccount = (
                utils.first([x for x in items if x.name.lower() == target_name]) or items[0]
            )

    def _sign(self, type_name: str, message: dict[str, Any]) -> str:
        assert self._domain is not None and self._types is not None
        domain = {**self._domain, "chainId": int(self._domain["chainId"])}
        signable = encode_typed_data(
            full_message={
                "types": {
                    "EIP712Domain": self._types["EIP712Domain"],
                    type_name: self._types[type_name],
                },
                "primaryType": type_name,
                "domain": domain,
                "message": message,
            }
        )
        return "0x" + self.owner.sign_message(signable).signature.hex()

    def _symbol(self, product_id: str) -> str:
        if self._products_cache and product_id in self._products_cache:
            return self._products_cache[product_id].base_token
        return product_id[:8]

    async def _product(self, symbol: str) -> Product:
        await self.products()
        assert self._products_cache is not None
        for p in self._products_cache.values():
            if p.base_token == symbol:
                return p
        raise FatalError(f"Unknown symbol: {symbol}")

    # MARK: Account

    async def registered(self) -> bool:
        res = await self._call("GET", "/subaccount", params={"sender": self.address})
        return len(res.get("data", [])) > 0

    async def balance(self) -> Decimal:
        await self._ensure_auth()
        assert self._subaccount is not None
        res = await self._call(
            "GET", "/subaccount/balance", params={"subaccountId": self._subaccount.id}
        )
        items = [BalanceItem(**x) for x in res.get("data", [])]
        usd = utils.first([x for x in items if x.tokenName.upper() == "USD"])
        return usd.available if usd else sum((x.available for x in items), Decimal(0))

    async def products(self) -> dict[str, Product]:
        if self._products_cache is not None:
            return self._products_cache
        res = await self._call("GET", "/product")
        self._products_cache = {}
        for x in res.get("data", []):
            p = Product(**x)
            self._products_cache[p.id] = p
        return self._products_cache

    async def points(self) -> PointsInfo:
        res = await self._call("GET", "/points/summary", params={"address": self.address})
        data = res.get("data", [])
        if not data:
            return PointsInfo(totalPoints=Decimal(0), referralPoints=Decimal(0), rank=0, tier=0)
        return PointsInfo(**data[0])

    async def points_history(self, season: int = 1, epoch: int = 2) -> list[PointsRecord]:
        params = {"address": self.address, "season": season, "epoch": epoch}
        res = await self._call("GET", "/points", params=params)
        return [PointsRecord(**x) for x in res.get("data", [])]

    async def fills(self, limit: int = 200) -> list[Fill]:
        await self._ensure_auth()
        await self.products()
        assert self._subaccount is not None
        items, cursor, remaining = [], None, limit

        while remaining > 0:
            batch = min(remaining, 200)
            params: dict[str, Any] = {
                "subaccountId": self._subaccount.id,
                "limit": batch,
                "orderBy": "createdAt",
                "order": "desc",
            }
            if cursor:
                params["cursor"] = cursor
            res = await self._call("GET", "/order/fill", params=params)
            for x in res.get("data", []):
                x["side"] = "buy" if x.get("side") == 0 else "sell"
                fill = Fill(**x)
                fill.symbol = self._symbol(fill.product_id)
                items.append(fill)
            remaining -= batch
            if not res.get("hasNext"):
                break
            cursor = res.get("nextCursor")
        return items

    async def total_volume(self) -> Decimal:
        fills = await self.fills(limit=2000)
        return sum((f.filled * f.price for f in fills), Decimal(0))

    async def total_fees(self) -> Decimal:
        fills = await self.fills(limit=2000)
        return sum((f.fee_usd for f in fills), Decimal(0))

    # MARK: Market data

    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        product = await self._product(symbol)
        res = await self._call("GET", "/product/market-price", params={"productIds": product.id})
        data = res.get("data", [])
        if not data:
            raise ApiError(f"No market price for {symbol}")
        item = data[0]
        return Decimal(item["bestBidPrice"]), Decimal(item["bestAskPrice"])

    async def get_price(self, symbol: str) -> Decimal:
        bid, ask = await self.get_bbo(symbol)
        return (bid + ask) / 2

    async def get_lot_size(self, symbol: str) -> Decimal:
        return (await self._product(symbol)).lot_size

    async def get_tick_size(self, symbol: str) -> Decimal:
        return (await self._product(symbol)).tick_size

    async def get_symbols(self) -> list[str]:
        prods = await self.products()
        return [p.base_token for p in prods.values()]

    # MARK: Positions

    async def positions(self, open_only: bool = True, limit: int = 200) -> list[Position]:
        raw = await self.raw_positions(open_only, limit)
        return [
            Position(
                id=p.id,
                symbol=p.symbol,
                side=_from_native(p.side),
                size=abs(p.size),
                entry_price=abs(p.cost / p.size) if p.size != 0 else Decimal(0),
            )
            for p in raw
            if p.size != 0
        ]

    async def raw_positions(self, open_only: bool = True, limit: int = 200) -> list[ApiPosition]:
        await self._ensure_auth()
        await self.products()
        assert self._subaccount is not None
        items, cursor, remaining = [], None, limit

        while remaining > 0:
            batch = min(remaining, 200)
            params: dict[str, Any] = {
                "subaccountId": self._subaccount.id,
                "open": str(open_only).lower(),
                "limit": batch,
            }
            if cursor:
                params["cursor"] = cursor
            res = await self._call("GET", "/position", params=params)
            for x in res.get("data", []):
                x["side"] = "buy" if x.get("side") == 0 else "sell"
                if open_only and Decimal(x.get("size", 0)) == 0:
                    continue
                pos = ApiPosition(**x)
                pos.symbol = self._symbol(pos.product_id)
                items.append(pos)
            remaining -= batch
            if not res.get("hasNext"):
                break
            cursor = res.get("nextCursor")
        return items

    async def close_position(self, position: Position) -> bool:
        if position.size == 0:
            return True
        close_side: Side = "ask" if position.side == "bid" else "bid"
        await self.market_order(position.symbol, close_side, position.size, reduce_only=True)
        return True

    async def close_all_positions(self) -> int:
        positions = await self.positions()
        for p in positions:
            await self.close_position(p)
        return len(positions)

    # MARK: Orders

    async def get_order(self, order_id: str) -> Order | None:
        await self._ensure_auth()
        await self.products()
        try:
            res = await self._call("GET", f"/order/{order_id}")
            res["side"] = "buy" if res.get("side") == 0 else "sell"
            raw = ApiOrder(**res)
            return Order(
                id=raw.id,
                symbol=self._symbol(raw.product_id),
                side=_from_native(raw.side),
                size=raw.quantity,
                filled=raw.filled_quantity,
                price=raw.price,
                status=to_domain_status(raw.status),
                reduce_only=raw.reduce_only,
            )
        except ApiError:
            return None

    async def market_order(
        self, symbol: str, side: Side, qty: Decimal, reduce_only: bool = False
    ) -> Order:
        await self._ensure_auth()
        product = await self._product(symbol)
        assert self._subaccount is not None

        native = _to_native(side)
        side_int = 0 if native == "buy" else 1
        nonce = str(time.time_ns())
        now_sec = int(time.time())

        data = {
            "sender": self.address,
            "subaccount": self._subaccount.name,
            "quantity": str(qty),
            "reduceOnly": reduce_only,
            "side": side_int,
            "engineType": 0,
            "onchainId": product.onchain_id,
            "nonce": nonce,
            "signedAt": now_sec,
            "type": "MARKET",
        }
        sign_data = {
            "sender": self.address,
            "subaccount": self._subaccount.name,
            "quantity": int(qty * Decimal("1e9")),
            "price": 0,
            "reduceOnly": reduce_only,
            "side": side_int,
            "engineType": 0,
            "productId": product.onchain_id,
            "nonce": int(nonce),
            "signedAt": now_sec,
        }
        signature = self._sign("TradeOrder", sign_data)
        res = await self._call("POST", "/order", json={"data": data, "signature": signature})

        order = await self.get_order(res.get("id", ""))
        if order is None:
            raise ApiError(f"Order {res.get('id')} not found")
        return order

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only: bool = False
    ) -> Order:
        await self._ensure_auth()
        product = await self._product(symbol)
        assert self._subaccount is not None

        native = _to_native(side)
        side_int = 0 if native == "buy" else 1
        nonce = str(time.time_ns())
        now_sec = int(time.time())

        data = {
            "sender": self.address,
            "subaccount": self._subaccount.name,
            "quantity": str(qty),
            "price": str(price),
            "reduceOnly": reduce_only,
            "postOnly": False,
            "timeInForce": "GTD",
            "side": side_int,
            "engineType": 0,
            "onchainId": product.onchain_id,
            "nonce": nonce,
            "signedAt": now_sec,
            "type": "LIMIT",
        }
        sign_data = {
            "sender": self.address,
            "subaccount": self._subaccount.name,
            "quantity": int(qty * Decimal("1e9")),
            "price": int(price * Decimal("1e9")),
            "reduceOnly": reduce_only,
            "side": side_int,
            "engineType": 0,
            "productId": product.onchain_id,
            "nonce": int(nonce),
            "signedAt": now_sec,
        }
        signature = self._sign("TradeOrder", sign_data)
        res = await self._call("POST", "/order", json={"data": data, "signature": signature})

        order = await self.get_order(res.get("id", ""))
        if order is None:
            raise ApiError(f"Order {res.get('id')} not found")
        return order

    async def cancel_order(self, order: Order) -> bool:
        await self._ensure_auth()
        assert self._subaccount is not None
        nonce = str(time.time_ns())
        data = {"sender": self.address, "subaccount": self._subaccount.name, "nonce": nonce}
        order_id_bytes = "0x" + order.id.replace("-", "").ljust(64, "0")
        sign_data = {**data, "orderIds": [order_id_bytes]}
        signature = self._sign("CancelOrder", sign_data)
        payload = {"data": {**data, "orderIds": [order.id]}, "signature": signature}
        res = await self._call("POST", "/order/cancel", json=payload)
        results = res.get("data", [])
        return len(results) > 0 and results[0].get("result") == "Ok"

    async def cancel_all_orders(self) -> int:
        await self._ensure_auth()
        assert self._subaccount is not None
        params: dict[str, Any] = {
            "subaccountId": self._subaccount.id,
            "limit": 200,
            "isWorking": "true",
        }
        res = await self._call("GET", "/order", params=params)
        cancelled = 0
        for x in res.get("data", []):
            dummy = Order(
                id=x["id"],
                symbol="",
                side="bid",
                size=Decimal(0),
                filled=Decimal(0),
                price=None,
                status=OrderStatus.OPEN,
            )
            if await self.cancel_order(dummy):
                cancelled += 1
        return cancelled

    # MARK: Leverage

    async def get_leverage(self, symbol: str) -> int | None:
        return None  # no API to fetch current leverage

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        pass  # leverage is not configurable via API on Ethereal


_cls_check: Type[TradingClient] = Client
