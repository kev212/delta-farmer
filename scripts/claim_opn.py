"""
Opinion Foundation OPN claim.

Подключается к API opinion.foundation, проверяет eligibility и клеймит OPN токены
для всех аккаунтов одновременно. Автоматически повторяет попытки при ошибках.

Запуск:
    uv run -m scripts.claim_opn
    uv run -m scripts.claim_opn -c configs/opinion.toml   # явно указать конфиг

Настройка:
    1. Скопировать пример конфига:
           cp configs.example/opinion.toml configs/opinion.toml

    2. Заполнить аккаунты в configs/opinion.toml:
           [[accounts]]
           name = "acc1"
           privkey = "0x..."
           # proxy = "host:port:user:pass"  # опционально

    Аккаунты можно скопировать из других конфигов (ethereal.toml, omni.toml и т.д.) —
    формат [[accounts]] с полями name / privkey / proxy совпадает.
"""

import argparse
import asyncio
import base64
import json
import os
import random
import string
import time
import tomllib
from decimal import Decimal

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.session import HttpMethod
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from pydantic import BaseModel, Field, SecretStr, field_validator

from lib.crypto import decrypt_value, is_encrypted
from lib.decorators import retry
from lib.http import parse_proxy

# MARK: Config

BASE_URL = "https://proxy.opinion.trade:8443/api/bsc/api"
DEFAULT_CONFIG = "configs/opinion.toml"

RETRY_DELAY = 5  # seconds between retries


class AccountConfig(BaseModel):
    name: str
    privkey: SecretStr = Field(repr=False)
    proxy: str | None = None

    @field_validator("privkey", mode="before")
    @classmethod
    def decrypt_privkey(cls, v: str) -> str:
        return decrypt_value(v) if is_encrypted(v) else v


def load_accounts(path: str) -> list[AccountConfig]:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return [AccountConfig(**rec) for rec in data["accounts"]]


# MARK: Auth


def _jwt_expired(token: str | None) -> bool:
    if not token:
        return True
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # fix padding
        data = json.loads(base64.b64decode(payload))
        return data.get("exp", 0) < time.time() + 60
    except Exception:
        return True


def _cache_path(wallet: str) -> str:
    os.makedirs(".cache", exist_ok=True)
    return f".cache/opn_foundation_jwt_{wallet[:10].lower()}"


def _load_jwt(wallet: str) -> str | None:
    path = _cache_path(wallet)
    try:
        return open(path).read().strip() or None
    except FileNotFoundError:
        return None


def _save_jwt(wallet: str, token: str) -> None:
    open(_cache_path(wallet), "w").write(token)


async def _auth(session: AsyncSession, acc: LocalAccount) -> str:
    wallet = acc.address

    # Step 1: get challenge message
    resp = await session.get(f"{BASE_URL}/v2/foundation/auth/challenge", params={"address": wallet})
    data = resp.json()
    if data["errno"] != 0:
        raise RuntimeError(f"challenge error {data['errno']}: {data.get('errmsg')}")
    message = data["result"]["message"]

    # Step 2: sign and verify
    sig = acc.sign_message(encode_defunct(text=message))
    resp = await session.post(
        f"{BASE_URL}/v2/foundation/auth/verify",
        json={"message": message, "signature": "0x" + sig.signature.hex()},
    )
    data = resp.json()
    if data["errno"] != 0:
        raise RuntimeError(f"verify error {data['errno']}: {data.get('errmsg')}")

    token = data["result"]["token"]
    _save_jwt(wallet, token)
    print(f"[{wallet}] Authenticated")
    return token


async def get_token(session: AsyncSession, acc: LocalAccount) -> str:
    wallet = acc.address
    token = _load_jwt(wallet)
    if _jwt_expired(token):
        print(f"[{wallet}] Authenticating...")
        token = await _auth(session, acc)
    return token  # type: ignore[return-value]


# MARK: Claim


def _make_fingerprint(wallet: str) -> str:
    rng = random.Random(wallet.lower())
    return "".join(rng.choices(string.hexdigits.lower(), k=32))


def _make_session(proxy: str | None) -> AsyncSession:
    return AsyncSession(
        impersonate="chrome",
        proxy=parse_proxy(proxy),
        verify=False,
        headers={
            "origin": "https://opinion.foundation",
            "referer": "https://opinion.foundation/",
        },
    )


async def _api(session: AsyncSession, method: HttpMethod, path: str, token: str, **kwargs) -> dict:
    headers = kwargs.pop("headers", {})
    headers["authorization"] = f"Bearer {token}"
    resp = await session.request(method, f"{BASE_URL}{path}", headers=headers, **kwargs)
    data = resp.json()
    if data["errno"] != 0:
        errno = data["errno"]
        errmsg = data.get("errmsg") or resp.text[:300]
        raise RuntimeError(f"API error {errno}: {errmsg}")
    return data["result"]


@retry(max_attempts=10, delay=5.0, backoff=1.0)
async def fetch_eligibility(session: AsyncSession, token: str) -> tuple[bool, bool]:
    """Returns (eligible, hasClaimed)."""
    result = await _api(session, "GET", "/v2/foundation/claim/eligibility", token)
    return bool(result.get("eligible")), bool(result.get("hasClaimed"))


@retry(max_attempts=10, delay=5.0, backoff=1.0)
async def fetch_claim_amount(session: AsyncSession, token: str) -> str:
    result = await _api(session, "GET", "/v2/foundation/claim/status", token)
    raw = int(result["claimAmount"])
    amount = Decimal(raw) / Decimal(10**18)
    return f"{amount:.4f}".rstrip("0").rstrip(".")


def build_consent(wallet: str, amount: str) -> str:
    return (
        f"Opinion Foundation Claim\n\n"
        f"Action: Claim\n"
        f"Amount: {amount} OPN\n\n"
        f"I have read and agree to the Opinion Foundation Terms of Service.\n\n"
        f"Wallet: {wallet}"
    )


async def claim(private_key: str, proxy: str | None) -> None:
    acc: LocalAccount = Account.from_key(private_key)
    wallet = acc.address

    session = _make_session(proxy)
    session.headers["x-device-fingerprint"] = _make_fingerprint(wallet)

    while True:
        try:
            token = await get_token(session, acc)

            eligible, claimed = await fetch_eligibility(session, token)
            if not eligible:
                print(f"[{wallet}] Not eligible, skipping")
                return
            if claimed:
                print(f"[{wallet}] Already claimed, skipping")
                return

            amount = await fetch_claim_amount(session, token)
            print(f"[{wallet}] Claimable: {amount} OPN")

            message = build_consent(wallet, amount)
            signature = "0x" + acc.sign_message(encode_defunct(text=message)).signature.hex()

            try:
                result = await _api(
                    session,
                    "POST",
                    "/v2/foundation/claim/submit",
                    token,
                    json={"consentMessage": message, "consentSignature": signature},
                )
                print(f"[{wallet}] Claimed — {result}")
            except RuntimeError as e:
                if "16001" in str(e):
                    print(f"[{wallet}] Already claimed")
                else:
                    raise
            return

        except Exception as e:
            print(f"[{wallet}] Error: {e}, retrying in {RETRY_DELAY}s...")

        await asyncio.sleep(RETRY_DELAY)


# MARK: Main


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    args = parser.parse_args()

    accs = load_accounts(args.config)
    print(f"Loaded {len(accs)} account(s) from {args.config}")
    await asyncio.gather(*[claim(acc.privkey.get_secret_value(), acc.proxy) for acc in accs])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
