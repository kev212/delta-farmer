# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Type

from eth_account.messages import encode_defunct
from pydantic import BaseModel

from lib import utils
from lib.decorators import bind_log_context
from lib.http import ApiError, AsyncHttp
from strategy import Position, ProfileInfo, TradingClient

from .hyperliquid import HyperLiquidClient

PRIVY_API = "https://auth.privy.io"
ARJUNA_API = "https://arjuna-production.up.railway.app"
ONYX_APP = "https://app.onyx.live"
PRIVY_APP_ID = "cmcmc1m1t012bl80npg0gl99u"

_PRIVY_HEADERS = {
    "privy-app-id": PRIVY_APP_ID,
    "privy-ca-id": "b98336a6-ca35-4d8a-a3a9-93e6cb54ab6f",
    "privy-client": "react-auth:3.17.0",
    "origin": ONYX_APP,
}

_ONYX_BUILDER = {"b": "0xb290f2f3fad4e540d0550985951cdad2711ac34a", "f": 10}


# MARK: Models


class OnyxAccountSummary(BaseModel):
    totalVolume: Decimal = Decimal(0)
    totalFees: Decimal = Decimal(0)
    totalPnl: Decimal = Decimal(0)
    onyxVolume: Decimal = Decimal(0)
    onyxBoostedVolume: Decimal = Decimal(0)
    onyxNonBoostedVolume: Decimal = Decimal(0)
    onyxTradeCount: int = 0


class OnyxUserInfo(BaseModel):
    boostedWalletAddress: str | None = None
    eoaAddress: str | None = None
    accountSummary: OnyxAccountSummary = OnyxAccountSummary()


# MARK: Client


@bind_log_context
class OnyxClient(HyperLiquidClient):
    """Onyx is not a DEX — it has no own clearinghouse or markets.
    It injects a builder fee into every order to attribute volume on Onyx's side.
    Positions live on the underlying market (native HL, xyz, etc.) as usual.
    Specify symbols with market prefix in config: "xyz:TSLA", "BTC".
    """

    exchange = "onyx"
    dex_prefix = ""
    _builder = _ONYX_BUILDER
    _symbols: list[str] = []

    def _filter_positions(self, positions: list[Position]) -> list[Position]:
        explicit = {s for s in self._symbols if s.startswith("hyna:")}
        return [p for p in positions if not p.symbol.startswith("hyna:") or p.symbol in explicit]

    @classmethod
    def __type_check(cls) -> Type[TradingClient]:
        return OnyxClient

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        super().__init__(name, privkey, proxy)
        self._privy_http = AsyncHttp(baseurl=PRIVY_API, headers=_PRIVY_HEADERS, proxy=proxy)
        self._arjuna_http = AsyncHttp(baseurl=ARJUNA_API, headers={"origin": ONYX_APP}, proxy=proxy)
        self._jwt: str | None = None
        self._login_lock = asyncio.Lock()

    # MARK: Auth

    async def _login(self) -> str:
        rep = await self._privy_http.request(
            "POST", "/api/v1/siwe/init", json={"address": self.address}
        )
        if not rep.ok:
            raise ApiError("Privy nonce failed", rep)
        nonce = rep.json()["nonce"]

        issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        message = (
            f"app.onyx.live wants you to sign in with your Ethereum account:\n"
            f"{self.address}\n\n"
            f"By signing, you are proving you own this wallet and logging in. "
            f"This does not initiate a transaction or cost any fees.\n\n"
            f"URI: https://app.onyx.live\n"
            f"Version: 1\n"
            f"Chain ID: 42161\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {issued_at}\n"
            f"Resources:\n"
            f"- https://privy.io"
        )
        signed = self.account.sign_message(encode_defunct(text=message))
        signature = "0x" + signed.signature.hex()

        rep = await self._privy_http.request(
            "POST",
            "/api/v1/siwe/authenticate",
            json={
                "message": message,
                "signature": signature,
                "chainId": "eip155:42161",
                "walletClientType": "python_bot",
                "connectorType": "injected",
                "mode": "login-or-sign-up",
            },
        )
        if not rep.ok:
            raise ApiError("Privy SIWE auth failed", rep)

        jwt: str = rep.json()["token"]
        self._jwt = jwt
        return jwt

    async def _authed_get(self, path: str, **kwargs) -> dict:
        if not self._jwt:
            async with self._login_lock:
                if not self._jwt:
                    await self._login()
        jwt = self._jwt
        rep = await self._arjuna_http.request(
            "GET", path, headers={"Authorization": f"Bearer {jwt}"}, **kwargs
        )
        if rep.status_code == 401:
            self._jwt = None
            async with self._login_lock:
                if not self._jwt:
                    await self._login()
            jwt = self._jwt
            rep = await self._arjuna_http.request(
                "GET", path, headers={"Authorization": f"Bearer {jwt}"}, **kwargs
            )
        if not rep.ok:
            raise ApiError(f"Arjuna GET {path} failed", rep)
        return rep.json()

    # MARK: Arjuna API

    async def user_info(self) -> OnyxUserInfo:
        data = await self._authed_get("/me/user")
        return OnyxUserInfo.model_validate(data)

    # MARK: Profile

    async def profile(self) -> ProfileInfo:
        bal, info = await asyncio.gather(self.balance(), self.user_info())
        s = info.accountSummary

        # Keep Onyx aligned with other apps: `info` shows burn as `-pnl`.
        addr = utils.short_addr(self.address)
        return ProfileInfo(
            addr=addr, balance=bal, volume=s.onyxVolume, pnl=s.totalPnl, points=Decimal(0)
        )
