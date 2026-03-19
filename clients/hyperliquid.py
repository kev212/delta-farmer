# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
import struct
import time
from decimal import Decimal
from math import floor, log10
from typing import Any, Type

import msgpack
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from web3 import Web3

from lib import utils
from lib.decorators import bind_log_context, ttl_cache
from lib.http import ApiError, AsyncHttp
from lib.logger import logger
from strategy import Order, OrderStatus, Position, ProfileInfo, Side, TradingClient

HL_API = "https://api.hyperliquid.xyz"

_AGENT_DOMAIN = {
    "name": "Exchange",
    "version": "1",
    "chainId": 1337,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}
_AGENT_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Agent": [
        {"name": "source", "type": "string"},
        {"name": "connectionId", "type": "bytes32"},
    ],
}


def _fmt(x: Decimal) -> str:
    """Format decimal for wire: normalize then no scientific notation."""
    return f"{Decimal(f'{x:.8f}').normalize():f}"


# MARK: Client


@bind_log_context
class HyperLiquidClient:
    exchange: str  # set by subclass
    dex_prefix: str = ""  # "" = standard HL, "hyna" etc. for HIP-3

    @classmethod
    def __type_check(cls) -> Type[TradingClient]:
        return HyperLiquidClient  # type: ignore

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        self.name = name
        self.account: LocalAccount = Account.from_key(privkey)
        self.address: str = self.account.address
        self.http = AsyncHttp(
            baseurl=HL_API,
            headers={"Content-Type": "application/json"},
            proxy=proxy,
        )

    # MARK: Infra

    def _coin(self, symbol: str) -> str:
        return f"{self.dex_prefix}:{symbol}" if self.dex_prefix else symbol

    def _strip(self, coin: str) -> str:
        prefix = f"{self.dex_prefix}:"
        return coin[len(prefix) :] if self.dex_prefix and coin.startswith(prefix) else coin

    @property
    def _dex(self) -> dict:
        return {"dex": self.dex_prefix} if self.dex_prefix else {}

    async def _info(self, **kwargs: Any) -> Any:
        rep = await self.http.request("POST", "/info", json=kwargs)
        if not rep.ok:
            raise ApiError(f"Info error {rep.status_code}: {rep.text[:200]}")
        return rep.json()

    def _sign_l1_action(self, action: dict, nonce: int) -> dict:
        data: bytes = msgpack.packb(action, use_bin_type=True)  # type: ignore[assignment]
        data += struct.pack(">Q", nonce)
        data += b"\x00"
        connection_id = Web3.keccak(data)
        payload = {
            "types": _AGENT_TYPES,
            "domain": _AGENT_DOMAIN,
            "primaryType": "Agent",
            "message": {"source": "a", "connectionId": connection_id},
        }
        signed = self.account.sign_message(encode_typed_data(full_message=payload))
        r, s, v = signed.r, signed.s, signed.v
        return {"r": f"0x{r:064x}", "s": f"0x{s:064x}", "v": v}

    async def _exchange(self, action: dict) -> dict:
        nonce = int(time.time() * 1000)
        sig = self._sign_l1_action(action, nonce)
        rep = await self.http.request(
            "POST", "/exchange", json={"action": action, "nonce": nonce, "signature": sig}
        )
        if not rep.ok:
            raise ApiError(f"Exchange error {rep.status_code}: {rep.text[:200]}")
        res = rep.json()
        if res is None:
            raise ApiError(f"Exchange returned null: {rep.text[:200]}")
        if res.get("status") != "ok":
            raise ApiError(f"Exchange rejected: {res}")
        return res

    # MARK: Metadata

    @ttl_cache(3600)
    async def _meta(self) -> dict:
        return await self._info(type="meta", **self._dex)

    @ttl_cache(3600)
    async def _dex_index(self) -> int:
        dexs = await self._info(type="perpDexs")
        for i, dex in enumerate(dexs):
            if dex and dex.get("name") == self.dex_prefix:
                return i  # perpDexs[i] maps directly to allPerpMetas[i]
        raise ApiError(f"DEX not found in perpDexs: {self.dex_prefix}")

    async def _asset_id(self, symbol: str) -> int:
        meta = await self._meta()
        coin = self._coin(symbol)
        for local_idx, asset in enumerate(meta["universe"]):
            if asset["name"] == coin:
                if not self.dex_prefix:
                    return local_idx
                dex_idx = await self._dex_index()
                return dex_idx * 10000 + 100000 + local_idx
        raise ApiError(f"Symbol not found: {symbol}")

    @ttl_cache(5)
    async def _asset_ctxs(self) -> list[dict]:
        rep = await self._info(type="metaAndAssetCtxs", **self._dex)
        return rep[1]

    async def _asset_ctx(self, symbol: str) -> dict:
        meta = await self._meta()
        ctxs = await self._asset_ctxs()
        coin = self._coin(symbol)
        for i, asset in enumerate(meta["universe"]):
            if asset["name"] == coin:
                return ctxs[i]
        raise ApiError(f"Symbol not found: {symbol}")

    # MARK: Lifecycle

    async def warmup(self) -> None:
        pass

    async def registered(self) -> bool:
        rep = await self._info(type="legalCheck", user=self.address)
        return rep.get("userAllowed", False)

    # MARK: Market info

    @ttl_cache(3600)
    async def get_symbols(self) -> list[str]:
        meta = await self._meta()
        return [self._strip(a["name"]) for a in meta["universe"] if not a.get("isDelisted")]

    async def get_lot_size(self, symbol: str) -> Decimal:
        meta = await self._meta()
        coin = self._coin(symbol)
        for asset in meta["universe"]:
            if asset["name"] == coin:
                return Decimal(10) ** -asset["szDecimals"]
        raise ApiError(f"Symbol not found: {symbol}")

    async def get_tick_size(self, symbol: str) -> Decimal:
        mid = float(await self.get_price(symbol))
        return Decimal(10) ** (floor(log10(mid)) - 4)

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        return Decimal(10)

    # MARK: Prices

    @ttl_cache(5)
    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        ctx = await self._asset_ctx(symbol)
        impact = ctx["impactPxs"]
        return Decimal(str(impact[0])), Decimal(str(impact[1]))

    async def get_price(self, symbol: str) -> Decimal:
        ctx = await self._asset_ctx(symbol)
        return Decimal(str(ctx["midPx"]))

    # MARK: Account

    async def balance(self) -> Decimal:
        rep = await self._info(type="clearinghouseState", user=self.address)
        return Decimal(str(rep["marginSummary"]["accountValue"]))

    async def get_leverage(self, symbol: str) -> int | None:
        rep = await self._info(type="clearinghouseState", user=self.address, **self._dex)
        coin = self._coin(symbol)
        for pos in rep.get("assetPositions", []):
            p = pos["position"]
            if p["coin"] == coin:
                lev = p.get("leverage", {})
                val = int(lev.get("value", 0))
                return val if val else None
        return None

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        asset_id = await self._asset_id(symbol)
        action = {
            "type": "updateLeverage",
            "asset": asset_id,
            "isCross": False,
            "leverage": leverage,
        }  # noqa: E501
        await self._exchange(action)

    # MARK: Positions

    async def positions(self) -> list[Position]:
        rep = await self._info(type="clearinghouseState", user=self.address, **self._dex)
        result = []
        for pos in rep.get("assetPositions", []):
            p = pos["position"]
            szi = Decimal(str(p["szi"]))
            if szi == 0:
                continue
            symbol = self._strip(p["coin"])
            side: Side = "bid" if szi > 0 else "ask"
            entry_px = p.get("entryPx")
            result.append(
                Position(
                    id=symbol,
                    symbol=symbol,
                    side=side,
                    size=abs(szi),
                    entry_price=Decimal(str(entry_px)) if entry_px else Decimal(0),
                    unrealized_pnl=Decimal(str(p.get("unrealizedPnl", 0))),
                )
            )
        return result

    async def close_position(self, position: Position) -> bool:
        close_side: Side = "ask" if position.side == "bid" else "bid"
        await self.market_order(position.symbol, close_side, position.size, reduce_only=True)
        return True

    async def close_all_positions(self) -> int:
        positions = await self.positions()
        for p in positions:
            await self.close_position(p)
        return len(positions)

    # MARK: Orders

    async def _place_order(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        tif: str,
        reduce_only: bool = False,
    ) -> Order:
        asset_id = await self._asset_id(symbol)
        order_wire = {
            "a": asset_id,
            "b": side == "bid",
            "p": _fmt(price),
            "s": _fmt(qty),
            "r": reduce_only,
            "t": {"limit": {"tif": tif}},
        }
        res = await self._exchange({"type": "order", "orders": [order_wire], "grouping": "na"})
        statuses = res["response"]["data"]["statuses"]
        st = statuses[0]
        if "error" in st:
            raise ApiError(f"Order rejected: {st['error']}")

        if "filled" in st:
            filled_data = st["filled"]
            return Order(
                id=str(filled_data["oid"]),
                symbol=symbol,
                side=side,
                size=qty,
                filled=Decimal(str(filled_data.get("totalSz", qty))),
                price=Decimal(str(filled_data.get("avgPx", price))),
                status=OrderStatus.FILLED,
                reduce_only=reduce_only,
            )

        oid = str(st["resting"]["oid"])
        return Order(
            id=oid,
            symbol=symbol,
            side=side,
            size=qty,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
            reduce_only=reduce_only,
        )

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        mid, tick = await asyncio.gather(self.get_price(symbol), self.get_tick_size(symbol))
        slippage = Decimal("1.05") if side == "bid" else Decimal("0.95")
        price = utils.round_to_tick_size(mid * slippage, tick)
        return await self._place_order(symbol, side, qty, price, "FrontendMarket", reduce_only)

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        return await self._place_order(symbol, side, qty, price, "Gtc", reduce_only)

    async def get_order(self, order_id: str) -> Order | None:
        rep = await self._info(type="orderStatus", user=self.address, oid=int(order_id))
        if rep.get("status") != "order":
            return None
        o = rep["order"]["order"]
        status_str = rep["order"]["status"]
        if status_str == "filled":
            status = OrderStatus.FILLED
        elif status_str == "open":
            status = OrderStatus.OPEN
        else:
            status = OrderStatus.CANCELED
        sz = Decimal(str(o["sz"]))
        orig_sz = Decimal(str(o["origSz"]))
        return Order(
            id=order_id,
            symbol=self._strip(o["coin"]),
            side="bid" if o["side"] == "B" else "ask",
            size=orig_sz,
            filled=orig_sz - sz,
            price=Decimal(str(o["limitPx"])),
            status=status,
            reduce_only=o.get("reduceOnly", False),
        )

    async def cancel_order(self, order: Order) -> bool:
        asset_id = await self._asset_id(order.symbol)
        action = {"type": "cancel", "cancels": [{"a": asset_id, "o": int(order.id)}]}
        try:
            await self._exchange(action)
            return True
        except ApiError as e:
            logger.warning(f"cancel_order failed: {e}")
            return False

    async def cancel_all_orders(self) -> int:
        rep = await self._info(type="openOrders", user=self.address, **self._dex)
        orders = rep if isinstance(rep, list) else []
        if not orders:
            return 0
        cancels = []
        for o in orders:
            asset_id = await self._asset_id(self._strip(o["coin"]))
            cancels.append({"a": asset_id, "o": o["oid"]})
        await self._exchange({"type": "cancel", "cancels": cancels})
        return len(cancels)

    # MARK: Profile

    async def profile(self) -> ProfileInfo:
        bal, portfolio = await asyncio.gather(
            self.balance(),
            self._info(type="portfolio", user=self.address),
        )

        volume = Decimal(0)
        pnl = Decimal(0)
        for period_name, period_data in portfolio:
            if period_name == "perpAllTime":
                volume = Decimal(str(period_data.get("vlm", 0)))
                history = period_data.get("pnlHistory", [])
                pnl = Decimal(str(history[-1][1])) if history else Decimal(0)
                break

        return ProfileInfo(
            addr=utils.short_addr(self.address),
            balance=bal,
            volume=volume,
            pnl=pnl,
            points=Decimal(0),
        )
