# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
import base64
import json
import time
from decimal import Decimal
from typing import Self, Type

import base58
from cryptography.hazmat.primitives.asymmetric.ec import (
    ECDSA,
    SECP256K1,
    SECP256R1,
    derive_private_key,
    generate_private_key,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount

from lib import utils
from lib.decorators import bind_log_context, ttl_cache
from lib.http import ApiError, AsyncHttp
from lib.logger import logger
from lib.models import AccountConfig
from strategy import Order, OrderStatus, Position, ProfileInfo, Side, TradingClient

# Nord (01.xyz) protobuf schema — action types, field numbers, error codes, FillMode enum:
# https://zo-devnet.n1.xyz/schema.proto
# All error codes referenced in this file (e.g. 133, 140) are defined in the Error enum there.
# Other docs: https://docs.01.xyz/reference/rest-api

NORD_API = "https://zo-mainnet.n1.xyz"
TURNKEY_API = "https://api.turnkey.com"
TURNKEY_AUTHPROXY = "https://authproxy.turnkey.com"
TURNKEY_AUTHPROXY_CONFIG_ID = "5ded06a7-4de9-40ba-8574-8716f865cb02"


# MARK: Protobuf codec (minimal, hand-rolled for Nord actions)


def _vi(n: int) -> bytes:
    """Encode non-negative integer as protobuf varint."""
    if n == 0:
        return b"\x00"
    out = []
    while n:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
    return bytes(out)


def _fv(field: int, val: int) -> bytes:
    return _vi((field << 3) | 0) + _vi(val)


def _fb(field: int, val: bytes) -> bytes:
    return _vi((field << 3) | 2) + _vi(len(val)) + val


def _read_vi(data: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return result, pos


def _parse_fields(data: bytes) -> dict[int, list]:
    """Parse protobuf message into {field_num: [values]}."""
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(data):
        tag, pos = _read_vi(data, pos)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            val, pos = _read_vi(data, pos)
        elif wire == 2:
            length, pos = _read_vi(data, pos)
            val = data[pos : pos + length]
            pos += length
        else:
            break  # unsupported wire type, stop
        fields.setdefault(field, []).append(val)
    return fields


def _read_receipt(data: bytes) -> dict:
    """Decode Receipt from Nord /action response."""
    payload_len, pos = _read_vi(data, 0)
    payload = data[pos : pos + payload_len]

    f = _parse_fields(payload)
    result: dict = {}

    if 1 in f:
        result["action_id"] = f[1][0]
    if 32 in f:
        result["error"] = f[32][0]
        return result
    if 33 in f:  # CreateSessionResult
        sf = _parse_fields(f[33][0])
        result["session_id"] = sf.get(1, [0])[0]
    if 34 in f:  # PlaceOrderResult
        result["place_result"] = _parse_place_result(f[34][0])
    if 35 in f:  # CancelOrderResult
        result["cancelled"] = True

    return result


def _parse_place_result(data: bytes) -> dict:
    f = _parse_fields(data)
    result: dict = {"order_id": None, "fills": [], "account_id": None}

    if 1 in f:  # Posted
        pf = _parse_fields(f[1][0])
        result["order_id"] = pf.get(5, [None])[0]
        result["account_id"] = pf.get(6, [None])[0]
        result["posted_side"] = pf.get(1, [0])[0]
        result["posted_price"] = pf.get(3, [0])[0]
        result["posted_size"] = pf.get(4, [0])[0]

    for trade_bytes in f.get(2, []):  # Trade (fill)
        tf = _parse_fields(trade_bytes)
        result["fills"].append(
            {"order_id": tf.get(2, [0])[0], "price": tf.get(4, [0])[0], "size": tf.get(5, [0])[0]}
        )

    return result


# MARK: Turnkey


class ZeroOneTurnkey:
    def __init__(self, privkey: str, proxy: str | None = None):
        raw = bytes.fromhex(privkey.removeprefix("0x").zfill(64))
        self._evm_account: LocalAccount = Account.from_key(raw)
        secp_key = derive_private_key(int.from_bytes(raw, "big"), SECP256K1())
        self._evm_pubkey_hex = (
            secp_key.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint).hex()
        )

        self._ephem_key = generate_private_key(SECP256R1())
        self._ephem_pubkey_hex = (
            self._ephem_key.public_key()
            .public_bytes(Encoding.X962, PublicFormat.CompressedPoint)
            .hex()
        )

        headers: dict[str, str] = {}
        self._authproxy = AsyncHttp(baseurl=TURNKEY_AUTHPROXY, headers=headers, proxy=proxy)
        self._api = AsyncHttp(baseurl=TURNKEY_API, headers=headers, proxy=proxy)

    def _stamp_eip191(self, body: str) -> str:
        signed = self._evm_account.sign_message(encode_defunct(text=body))
        stamp = {
            "publicKey": self._evm_pubkey_hex,
            "scheme": "SIGNATURE_SCHEME_TK_API_SECP256K1_EIP191",
            "signature": encode_dss_signature(signed.r, signed.s).hex(),
        }
        return base64.b64encode(json.dumps(stamp, separators=(",", ":")).encode()).decode()

    def _stamp_p256(self, body: bytes) -> str:
        stamp = {
            "publicKey": self._ephem_pubkey_hex,
            "scheme": "SIGNATURE_SCHEME_TK_API_P256",
            "signature": self._ephem_key.sign(body, ECDSA(SHA256())).hex(),
        }
        return base64.b64encode(json.dumps(stamp, separators=(",", ":")).encode()).decode()

    async def _call(self, path: str, body: dict, *, eip191: bool = False) -> dict:
        data = json.dumps(
            {**body, "timestampMs": str(int(time.time() * 1000))}, separators=(",", ":")
        )
        if eip191:
            rep = await self._api.request(
                "POST",
                path,
                data=data,
                headers={"x-stamp": self._stamp_eip191(data), "Content-Type": "application/json"},
            )
        else:
            enc = data.encode()
            rep = await self._api.request(
                "POST",
                path,
                data=enc,
                headers={
                    "x-stamp": self._stamp_p256(enc),
                    "Content-Type": "text/plain;charset=UTF-8",
                    "x-client-version": "@turnkey/core@1.7.0",
                },
            )
        if not rep.ok:
            raise ApiError(f"Turnkey {path} failed", rep)
        return rep.json()

    @ttl_cache(86400)
    async def _get_org_id(self) -> str:
        rep = await self._authproxy.request(
            "POST",
            "/v1/account",
            json={"filterType": "PUBLIC_KEY", "filterValue": self._evm_pubkey_hex},
            headers={"x-auth-proxy-config-id": TURNKEY_AUTHPROXY_CONFIG_ID},
        )
        if not rep.ok:
            raise ApiError("Turnkey account lookup failed", rep)
        data = rep.json()
        if "organizationId" not in data:
            raise ApiError(f"Turnkey: wallet not registered on 01.xyz (response: {data})")
        return data["organizationId"]

    @ttl_cache(86400)
    async def login(self) -> str:
        """Authenticate via EVM key → Turnkey → returns Solana address."""
        org_id = await self._get_org_id()
        await self._call(
            "/public/v1/submit/stamp_login",
            {
                "parameters": {"publicKey": self._ephem_pubkey_hex, "expirationSeconds": "1209600"},
                "organizationId": org_id,
                "type": "ACTIVITY_TYPE_STAMP_LOGIN",
            },
            eip191=True,
        )
        res = await self._call(
            "/public/v1/query/list_wallet_accounts",
            {
                "organizationId": org_id,
                "includeWalletDetails": True,
                "paginationOptions": {"limit": "100"},
            },
        )
        for acc in res.get("accounts", []):
            if acc.get("curve") == "CURVE_ED25519":
                return acc["address"]
        raise ApiError("Turnkey Ed25519 wallet not found")

    async def sign_payload(self, payload_hex: str) -> bytes:
        """Sign Nord action payload (hex string) via Turnkey managed Ed25519 key."""
        org_id = await self._get_org_id()
        solana_addr = await self.login()
        res = await self._call(
            "/public/v1/submit/sign_raw_payload",
            {
                "parameters": {
                    "signWith": solana_addr,
                    "payload": payload_hex,
                    "encoding": "PAYLOAD_ENCODING_TEXT_UTF8",
                    "hashFunction": "HASH_FUNCTION_NOT_APPLICABLE",
                },
                "organizationId": org_id,
                "type": "ACTIVITY_TYPE_SIGN_RAW_PAYLOAD_V2",
            },
        )
        r = res["activity"]["result"]["signRawPayloadResult"]
        return bytes.fromhex(r["r"]) + bytes.fromhex(r["s"])


# MARK: Client


@bind_log_context
class ZeroOneClient:
    exchange = "zero1"

    @classmethod
    def __type_check(cls) -> Type[TradingClient]:
        return ZeroOneClient

    @classmethod
    def from_config(cls, cfg: AccountConfig) -> Self:
        return cls(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        self.name = name
        self.address = Account.from_key(privkey.removeprefix("0x").zfill(64)).address
        self._turnkey = ZeroOneTurnkey(privkey, proxy)
        self._session_key = Ed25519PrivateKey.generate()
        self._session_pubkey = self._session_key.public_key().public_bytes_raw()
        self._session_id: int | None = None
        self._account_id: int | None = None
        self._login_lock = asyncio.Lock()
        self.http = AsyncHttp(baseurl=NORD_API, headers={}, proxy=proxy)

    # MARK: Auth

    async def _get_server_ts(self) -> int:
        rep = await self.http.request("GET", "/timestamp")
        if not rep.ok:
            raise ApiError("Nord timestamp failed", rep)
        return int(rep.json())

    async def _login(self) -> None:
        solana_addr = await self._turnkey.login()
        # logger.info(f"Turnkey Solana address: {solana_addr}")

        rep = await self.http.request("GET", f"/user/{solana_addr}")
        if not rep.ok or not rep.json().get("accountIds"):
            addr = utils.short_addr(solana_addr)
            raise ApiError(
                f"Nord account not found for {addr} — connect wallet on 01.xyz first",
                rep if not rep.ok else None,
            )
        account_ids = rep.json()["accountIds"]
        self._account_id = account_ids[0]

        ts = await self._get_server_ts()
        user_pubkey = base58.b58decode(solana_addr)
        expiry = ts + 3600 * 24

        # CreateSession action (field 4)
        inner = _fb(1, user_pubkey) + _fb(2, self._session_pubkey) + _fv(3, expiry)
        action = _fv(1, ts) + _fb(4, inner)

        # user_sign: Turnkey signs hex(raw) with managed Ed25519 key
        raw = _vi(len(action)) + action
        sig = await self._turnkey.sign_payload(raw.hex())

        receipt = await self._submit_raw(action, sig)
        if "error" in receipt:
            raise ApiError(f"Nord CreateSession error code: {receipt['error']}")
        self._session_id = receipt.get("session_id")
        # logger.info(f"Nord session {self._session_id}, account {self._account_id}")

    async def _ensure_session(self) -> tuple[int, int]:
        if self._session_id is not None and self._account_id is not None:
            return self._session_id, self._account_id
        async with self._login_lock:
            if self._session_id is None:
                await self._login()
        assert self._session_id is not None and self._account_id is not None
        return self._session_id, self._account_id

    async def _submit_raw(self, action: bytes, sig: bytes) -> dict:
        raw = _vi(len(action)) + action
        rep = await self.http.request(
            "POST",
            "/action",
            data=raw + sig,
            headers={"Content-Type": "application/octet-stream"},
        )
        if not rep.ok:
            raise ApiError("Nord /action failed", rep)
        return _read_receipt(rep.content)

    async def _action(self, kind_field: int, kind_data: bytes, nonce: int = 0) -> dict:
        ts = await self._get_server_ts()
        action = _fv(1, ts)
        if nonce:
            action += _fv(2, nonce)
        action += _fb(kind_field, kind_data)
        raw = _vi(len(action)) + action
        sig = self._session_key.sign(raw)
        receipt = await self._submit_raw(action, sig)
        if "error" in receipt:
            raise ApiError(f"Nord action error code: {receipt['error']}")
        return receipt

    # MARK: Lifecycle

    async def warmup(self) -> None:
        await self._ensure_session()

    async def registered(self) -> bool:
        try:
            await self._ensure_session()
            return True
        except Exception:
            return False

    # MARK: Market info

    @ttl_cache(3600)
    async def _meta(self) -> dict:
        rep = await self.http.request("GET", "/info")
        if not rep.ok:
            raise ApiError("Nord /info failed", rep)
        return rep.json()

    async def _market(self, symbol: str) -> dict:
        info = await self._meta()
        sym = symbol.upper() + "USD"
        for m in info.get("markets", []):
            if m["symbol"] == sym:
                return m
        raise ApiError(f"Market not found: {symbol}")

    @ttl_cache(3600)
    async def get_symbols(self) -> list[str]:
        info = await self._meta()
        return [m["symbol"].removesuffix("USD") for m in info.get("markets", [])]

    async def get_lot_size(self, symbol: str) -> Decimal:
        m = await self._market(symbol)
        return Decimal(10) ** -m["sizeDecimals"]

    async def get_tick_size(self, symbol: str) -> Decimal:
        m = await self._market(symbol)
        return Decimal(10) ** -m["priceDecimals"]

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        return Decimal(10)

    # MARK: Prices

    @ttl_cache(5)
    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        m = await self._market(symbol)
        rep = await self.http.request("GET", f"/market/{m['marketId']}/orderbook")
        if not rep.ok:
            raise ApiError("Nord orderbook failed", rep)
        ob = rep.json()
        bid = Decimal(str(ob["bids"][0][0])) if ob.get("bids") else Decimal(0)
        ask = Decimal(str(ob["asks"][0][0])) if ob.get("asks") else Decimal(0)
        return bid, ask

    async def get_price(self, symbol: str) -> Decimal:
        bid, ask = await self.get_bbo(symbol)
        return (bid + ask) / 2

    # MARK: Account

    async def balance(self) -> Decimal:
        _, acc_id = await self._ensure_session()
        rep = await self.http.request("GET", f"/account/{acc_id}")
        if not rep.ok:
            raise ApiError("Nord account failed", rep)
        for bal in rep.json().get("balances", []):
            if bal["token"] == "USDC":
                return Decimal(str(bal["amount"]))
        return Decimal(0)

    async def get_leverage(self, symbol: str) -> int | None:
        return None  # Nord uses risk-based margin; leverage on 01.xyz is frontend-only

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        pass

    # MARK: Positions

    async def positions(self) -> list[Position]:
        _, acc_id = await self._ensure_session()
        rep = await self.http.request("GET", f"/account/{acc_id}")
        if not rep.ok:
            raise ApiError("Nord account failed", rep)

        info = await self._meta()
        market_map = {m["marketId"]: m for m in info.get("markets", [])}

        result = []
        for pos in rep.json().get("positions", []):
            perp = pos.get("perp")
            if not perp:
                continue
            base_size = Decimal(str(perp["baseSize"]))
            if base_size == 0:
                continue
            m = market_map.get(pos["marketId"], {})
            symbol = m.get("symbol", str(pos["marketId"])).removesuffix("USD")
            side: Side = "bid" if perp.get("isLong", base_size > 0) else "ask"
            result.append(
                Position(
                    id=str(pos["marketId"]),
                    symbol=symbol,
                    side=side,
                    size=abs(base_size),
                    entry_price=Decimal(str(perp.get("price", 0))),
                    unrealized_pnl=Decimal(str(perp.get("sizePricePnl", 0))),
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
        fill_mode: int,
        reduce_only: bool = False,
    ) -> Order:
        sess_id, acc_id = await self._ensure_session()
        m = await self._market(symbol)
        market_id = m["marketId"]
        price_dec, size_dec = m["priceDecimals"], m["sizeDecimals"]

        side_val = 1 if side == "bid" else 0
        price_int = int(price * Decimal(10) ** price_dec)
        size_int = int(qty * Decimal(10) ** size_dec)

        # PlaceOrder fields: session_id=1, market_id=2, side=3, fill_mode=4,
        # reduce_only=5, price=6, size=7, sender_account_id=34
        inner = (
            _fv(1, sess_id)
            + _fv(2, market_id)
            + _fv(3, side_val)
            + _fv(4, fill_mode)
            + _fv(5, int(reduce_only))
            + _fv(6, price_int)
            + _fv(7, size_int)
            + _fv(34, acc_id)
        )

        receipt = await self._action(7, inner)  # field 7 = place_order
        pr = receipt.get("place_result", {})
        fills = pr.get("fills", [])
        order_id = pr.get("order_id")

        if fills:
            total_qty = sum(
                (Decimal(str(f["size"])) * Decimal(10) ** -size_dec for f in fills), Decimal(0)
            )
            avg_px = sum(
                (Decimal(str(f["price"])) * Decimal(10) ** -price_dec for f in fills), Decimal(0)
            ) / len(fills)
            return Order(
                id=str(order_id or fills[0].get("order_id", 0)),
                symbol=symbol,
                side=side,
                size=qty,
                filled=total_qty,
                price=avg_px,
                status=OrderStatus.FILLED,
                reduce_only=reduce_only,
            )

        if order_id:
            return Order(
                id=str(order_id),
                symbol=symbol,
                side=side,
                size=qty,
                filled=Decimal(0),
                price=price,
                status=OrderStatus.OPEN,
                reduce_only=reduce_only,
            )

        raise ApiError(f"PlaceOrder: unexpected receipt: {receipt}")

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        bid, ask = await self.get_bbo(symbol)
        mid = (bid + ask) / 2
        m = await self._market(symbol)
        tick = Decimal(10) ** -m["priceDecimals"]
        slippage = Decimal("1.05") if side == "bid" else Decimal("0.95")
        price = utils.round_to_tick_size(mid * slippage, tick)
        return await self._place_order(symbol, side, qty, price, 2, reduce_only)  # IOC

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        if reduce_only:
            # Nord rejects LIMIT and POST_ONLY with reduce_only=True (error 140 always).
            # Only IOC works for closing. If price moved and IOC finds no counterpart
            # (error 133), fall back to market which uses BBO ± slippage and always fills.
            try:
                return await self._place_order(symbol, side, qty, price, 2, reduce_only)  # IOC
            except ApiError as e:
                if "133" not in str(e):  # IMMEDIATE_ORDER_GOT_NO_FILLS
                    raise
                logger.warning(f"limit_order {symbol}: IOC got no fills, falling back to market")
                return await self.market_order(symbol, side, qty, reduce_only)

        return await self._place_order(symbol, side, qty, price, 0, reduce_only)  # LIMIT

    async def get_order(self, order_id: str) -> Order | None:
        rep = await self.http.request("GET", f"/order/{order_id}")
        if not rep.ok:
            return None
        o = rep.json()
        symbol = o.get("marketSymbol", "?").removesuffix("USD")
        raw_size = Decimal(str(o.get("placedSize") or 0))
        filled = Decimal(str(o.get("filledSize") or 0))
        price = Decimal(str(o.get("placedPrice") or 0))
        side: Side = "bid" if o.get("side") == "bid" else "ask"

        reason = o.get("finalizationReason")
        if reason == "Canceled":
            status = OrderStatus.CANCELED
        elif filled > 0 and filled >= raw_size:
            status = OrderStatus.FILLED
        else:
            status = OrderStatus.OPEN

        return Order(
            id=order_id,
            symbol=symbol,
            side=side,
            size=raw_size,
            filled=filled,
            price=price,
            status=status,
        )

    async def cancel_order(self, order: Order) -> bool:
        sess_id, acc_id = await self._ensure_session()
        # CancelOrderById: session_id=1, order_id=2, sender_account_id=33
        inner = _fv(1, sess_id) + _fv(2, int(order.id)) + _fv(33, acc_id)
        try:
            await self._action(8, inner)  # field 8 = cancel_order_by_id
            return True
        except ApiError as e:
            logger.warning(f"cancel_order failed: {e}")
            return False

    async def cancel_all_orders(self) -> int:
        _, acc_id = await self._ensure_session()
        rep = await self.http.request("GET", f"/account/{acc_id}/orders", params={"pageSize": 100})
        if not rep.ok:
            return 0
        data = rep.json()
        items = data.get("items", data) if isinstance(data, dict) else data
        count = 0
        for o in items:
            oid = o.get("orderId")
            if not oid or o.get("finalizationReason"):
                continue
            dummy = Order(
                id=str(oid),
                symbol="?",
                side="bid",
                size=Decimal(0),
                filled=Decimal(0),
                price=None,
                status=OrderStatus.OPEN,
            )
            if await self.cancel_order(dummy):
                count += 1
        return count

    # MARK: Profile

    async def profile(self) -> ProfileInfo:
        _, acc_id = await self._ensure_session()
        rep = await self.http.request("GET", f"/account/{acc_id}")
        if not rep.ok:
            raise ApiError("Nord account failed", rep)
        account = rep.json()
        bal = Decimal(0)
        for b in account.get("balances", []):
            if b["token"] == "USDC":
                bal = Decimal(str(b["amount"]))

        pnl = Decimal(0)

        vol_rep = await self.http.request(
            "GET",
            "/account/volume",
            params={"accountId": acc_id, "since": "2020-01-01T00:00:00Z"},
        )
        vol = Decimal(0)
        if vol_rep.ok:
            for entry in vol_rep.json():
                vol += Decimal(str(entry.get("volumeQuote", 0)))

        pnl_rep = await self.http.request("GET", f"https://01.xyz/api/pnl-totals/{acc_id}")
        if pnl_rep.ok:
            pnl = Decimal(str(pnl_rep.json().get("totalPnl", 0)))

        return ProfileInfo(
            addr=utils.short_addr(self.address),
            balance=bal,
            volume=vol,
            pnl=pnl,
            points=Decimal(0),
        )
