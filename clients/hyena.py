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
from strategy import ProfileInfo, TradingClient

from .hyperliquid import HyperLiquidClient

HYENA_API = "https://app.hyena.trade"


# MARK: Models


class HyenaRewardBalance(BaseModel):
    enaxPoints: Decimal
    sats: Decimal
    gold: Decimal


class HyenaRank(BaseModel):
    tier: str
    percentile: float


class HyenaHistoryItem(BaseModel):
    id: str  # "reward-week-6" etc.
    enaxPoints: Decimal


class HyenaRewards(BaseModel):
    balance: HyenaRewardBalance
    rank: HyenaRank
    availableToClaim: Decimal
    history: list[HyenaHistoryItem] = []


class HyenaPayoutsTotal(BaseModel):
    totalClaimed: Decimal
    todayAmount: Decimal


# MARK: Client


@bind_log_context
class HyenaClient(HyperLiquidClient):
    exchange = "hyena"
    dex_prefix = "hyna"

    @classmethod
    def __type_check(cls) -> Type[TradingClient]:
        return HyenaClient

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        super().__init__(name, privkey, proxy)
        self._app_http = AsyncHttp(
            baseurl=HYENA_API,
            headers={"Origin": HYENA_API, "Referer": f"{HYENA_API}/"},
            proxy=proxy,
        )
        self._jwt: str | None = None
        self._login_lock = asyncio.Lock()

    # MARK: Auth

    async def _login(self) -> str:  # serialized via _login_lock
        rep = await self._app_http.request(
            "GET", "/api/auth/nonce", params={"address": self.address}
        )
        if not rep.ok:
            raise ApiError("Nonce failed", rep)
        nonce = rep.json()["nonce"]

        issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        message = (
            f"app.hyena.trade wants you to sign in with your Ethereum account:\n"
            f"{self.address}\n"
            f"\n"
            f"Sign in with Ethereum to the app.\n"
            f"\n"
            f"URI: https://app.hyena.trade\n"
            f"Version: 1\n"
            f"Chain ID: 42161\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {issued_at}"
        )
        signed = self.account.sign_message(encode_defunct(text=message))
        signature = "0x" + signed.signature.hex()

        rep = await self._app_http.request(
            "POST", "/api/auth", json={"message": message, "signature": signature}
        )
        if not rep.ok:
            raise ApiError("SIWE auth failed", rep)

        jwt: str = rep.json()["token"]
        self._jwt = jwt
        return jwt

    async def _authed_get(self, path: str, **kwargs) -> dict:
        if not self._jwt:
            async with self._login_lock:
                if not self._jwt:  # re-check after acquiring lock
                    await self._login()
        jwt = self._jwt
        rep = await self._app_http.request(
            "GET", path, headers={"Authorization": f"Bearer {jwt}"}, **kwargs
        )
        if rep.status_code == 401:
            self._jwt = None
            async with self._login_lock:
                if not self._jwt:
                    await self._login()
            jwt = self._jwt
            rep = await self._app_http.request(
                "GET", path, headers={"Authorization": f"Bearer {jwt}"}, **kwargs
            )
        if not rep.ok:
            raise ApiError(f"Hyena GET {path} failed", rep)
        return rep.json()

    # MARK: Account

    async def balance(self) -> Decimal:
        spot, perp = await asyncio.gather(
            self._info(type="spotClearinghouseState", user=self.address),
            self._info(type="clearinghouseState", user=self.address, dex=self.dex_prefix),
        )
        usde = next(
            (Decimal(str(b["total"])) for b in spot["balances"] if b["coin"] == "USDE"), Decimal(0)
        )
        dex_equity = Decimal(str(perp["marginSummary"]["accountValue"]))
        return usde + dex_equity

    # MARK: Points API

    async def rewards(self) -> HyenaRewards:
        data = await self._authed_get(
            f"/api/hyena/rewards/{self.address}", params={"page": 1, "limit": 50}
        )
        return HyenaRewards.model_validate(data["data"])

    async def payouts_total(self) -> HyenaPayoutsTotal:
        data = await self._authed_get("/api/hyena/payouts/total")
        return HyenaPayoutsTotal.model_validate(data)

    # MARK: Profile

    async def profile(self) -> ProfileInfo:
        bal, rewards, portfolio = await asyncio.gather(
            self.balance(),
            self.rewards(),
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
            points=rewards.balance.enaxPoints,
        )
