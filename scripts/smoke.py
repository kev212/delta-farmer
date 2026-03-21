# delta-farmer | https://github.com/vladkens/delta-farmer
# Smoke test – quick sanity check that the exchange API is still alive.
# Tests one account (first in config) with a symbol that must NOT be in config symbols.
# Usage: uv run scripts/smoke.py omni|pacifica SYMBOL SIZE_USD [-c xxx.toml]
import argparse
import asyncio
import glob
import sys
import time
from decimal import Decimal

from lib.utils import round_to_tick_size
from strategy import TradingClient, usd_to_qty

PASS = "✓"
FAIL = "✗"
SKIP = "~"


async def smoke(client: TradingClient, symbol: str, size_usd: float) -> tuple[int, int]:
    passed, failed = 0, 0

    def report(label: str, ok: bool, note: str = "") -> bool:
        nonlocal passed, failed
        icon = PASS if ok else FAIL
        suffix = f"  ({note})" if note else ""
        print(f"  {icon} {label}{suffix}")
        if ok:
            passed += 1
        else:
            failed += 1
        return ok

    # MARK: Lifecycle

    try:
        await client.warmup()
        report("warmup", True)
    except Exception as e:
        report("warmup", False, str(e))
        return passed, failed  # can't continue without auth

    # MARK: Read-only checks

    try:
        bal = await client.balance()
        report("balance", True, f"${bal:,.2f}")
    except Exception as e:
        report("balance", False, str(e))

    price = lot = tick = None
    try:
        price, lot, tick = await asyncio.gather(
            client.get_price(symbol),
            client.get_lot_size(symbol),
            client.get_tick_size(symbol),
        )
        report("market info", True, f"{symbol} ${price:,.2f}  lot={lot}  tick={tick}")
    except Exception as e:
        report("market info", False, str(e))

    assert lot is not None, "lot size is not set"
    assert tick is not None, "tick size is not set"
    assert price is not None, "price is not set"

    if price is None:
        print(f"  {SKIP} skipping order tests (no market data)")
        return passed, failed

    try:
        min_usd = await client.get_min_trade_usd(symbol)
        ok = size_usd >= float(min_usd)
        note = f"min=${min_usd}"
        if not ok:
            note += f"  ⚠ requested ${size_usd} is below minimum"
        report("min_trade_usd", ok, note)
        if not ok:
            print(f"  {SKIP} skipping order tests (size below minimum)")
            return passed, failed
    except Exception as e:
        report("min_trade_usd", False, str(e))

    try:
        pre = await client.positions()
        warn = "  ⚠ account not clean" if pre else ""
        report("positions baseline", True, f"{len(pre)} open{warn}")
    except Exception as e:
        report("positions baseline", False, str(e))

    # MARK: Leverage

    try:
        lev_before = await client.get_leverage(symbol)
        TEST_LEVERAGE = 8 if lev_before != 8 else 10
        await client.set_leverage(symbol, TEST_LEVERAGE)
        lev_after = await client.get_leverage(symbol)
        if lev_after == TEST_LEVERAGE:
            report("set_leverage", True, f"{lev_before} → {lev_after}")
        elif lev_before == lev_after:
            print(
                f"  {SKIP} set_leverage  ({lev_before} → {lev_after}, no-op / session unavailable)"
            )
        else:
            report("set_leverage", False, f"{lev_before} → {lev_after}, expected {TEST_LEVERAGE}")
    except Exception as e:
        report("set_leverage", False, str(e))

    # MARK: Market order cycle

    qty = usd_to_qty(Decimal(str(size_usd)), price, lot)
    morder = None

    try:
        t = time.time()
        morder = await client.market_order(symbol, "bid", qty)
        report(
            "market_order bid",
            True,
            f"id={morder.id}  qty={qty}  filled={morder.filled}  {time.time() - t:.1f}s",
        )
    except Exception as e:
        report("market_order bid", False, str(e))

    if morder is not None:
        try:
            positions = await client.positions()
            found = any(p.symbol == symbol and p.side == "bid" for p in positions)
            report("position appeared", found, f"{len(positions)} open")
        except Exception as e:
            report("position appeared", False, str(e))

    if morder is not None:
        try:
            positions = await client.positions()
            pos = next((p for p in positions if p.symbol == symbol and p.side == "bid"), None)
            if pos:
                await client.close_position(pos)
            else:
                await client.market_order(symbol, "ask", qty, reduce_only=True)
            report("close_position", True)
        except Exception as e:
            report("close_position", False, str(e))

    if morder is not None:
        try:
            positions = await client.positions()
            still_open = any(p.symbol == symbol and p.side == "bid" for p in positions)
            report("position closed", not still_open)
        except Exception as e:
            report("position closed", False, str(e))

    # MARK: Limit order cycle

    # Place bid 10% below mid — won't fill in the few seconds before cancel.
    limit_px = round_to_tick_size(price * Decimal("0.9"), tick)
    limit_qty = usd_to_qty(Decimal(str(size_usd)), limit_px, lot)
    lorder = None

    try:
        t = time.time()
        lorder = await client.limit_order(symbol, "bid", limit_qty, limit_px)
        elapsed = time.time() - t

        if lorder.status.lower() == "filled":
            # Exchange has no native limit orders (e.g. Omni falls back to market internally)
            report(
                "limit_order bid",
                True,
                f"no native limit, executed as market  id={lorder.id}  {elapsed:.1f}s",
            )
            try:
                await client.market_order(symbol, "ask", qty, reduce_only=True)
            except Exception:
                pass
            lorder = None
        else:
            report(
                "limit_order bid",
                True,
                f"id={lorder.id}  status={lorder.status}  price={limit_px}  {elapsed:.1f}s",
            )
    except Exception as e:
        report("limit_order bid", False, str(e))

    if lorder is not None:
        try:
            fetched = await client.get_order(lorder.id)
            ok = fetched is not None and fetched.status.lower() in ("open", "pending", "new")
            note = f"status={fetched.status}" if fetched else "not found"
            report("get_order", ok, note)
        except Exception as e:
            report("get_order", False, str(e))

    if lorder is not None:
        try:
            ok = await client.cancel_order(lorder)
            report("cancel_order", ok)
        except Exception as e:
            report("cancel_order", False, str(e))

    if lorder is not None:
        try:
            fetched = await client.get_order(lorder.id)
            done = fetched is None or fetched.status.lower() in (
                "cancelled",
                "canceled",
                "rejected",
            )
            note = f"status={fetched.status}" if fetched else "not found (ok)"
            report("order cancelled", done, note)
        except Exception as e:
            report("order cancelled", False, str(e))

    return passed, failed


async def main():
    parser = argparse.ArgumentParser(prog="smoke", description="Smoke test for exchange clients")
    exchanges = ["ethereal", "hyena", "hyperliquid", "nado", "omni", "onyx", "pacifica", "zero1"]
    parser.add_argument("exchange", choices=exchanges)
    parser.add_argument("symbol", help="Symbol to test (must NOT be in config symbols)")
    parser.add_argument("size", type=float, help="Trade size in USD")
    parser.add_argument(
        "-c", "--config", default=None, help="Path to config file (auto-detected if omitted)"
    )
    args = parser.parse_args()

    if args.config is None:
        matches = sorted(glob.glob(f"configs/{args.exchange}*.toml"))
        if not matches:
            parser.error(
                f"no config file found in configs/ for '{args.exchange}', use -c to specify"
            )
        args.config = matches[0]
        print(f"config   : {args.config} (auto)")

    from apps.hyperliquid import HyperLiquidNativeClient
    from clients.ethereal import EtherealClient
    from clients.hyena import HyenaClient
    from clients.nado import NadoClient
    from clients.omni import OmniClient
    from clients.onyx import OnyxClient
    from clients.pacifica import PacificaClient
    from clients.zero1 import ZeroOneClient
    from strategy import StrategyConfig

    CLIENT_MAP = {
        "ethereal": EtherealClient,
        "hyena": HyenaClient,
        "hyperliquid": HyperLiquidNativeClient,
        "nado": NadoClient,
        "omni": OmniClient,
        "onyx": OnyxClient,
        "pacifica": PacificaClient,
        "zero1": ZeroOneClient,
    }
    if args.exchange not in CLIENT_MAP:
        parser.error(f"unsupported exchange '{args.exchange}'")

    cfg = StrategyConfig.load(args.config)
    acc_cfg = cfg.accounts[0]
    client = CLIENT_MAP[args.exchange].from_config(acc_cfg)  # type: ignore

    if args.symbol in cfg.symbols:
        parser.error(
            f"symbol '{args.symbol}' is in config symbols {cfg.symbols} — "
            "pick a different symbol to avoid conflicting with the running bot"
        )
    normalize = getattr(client, "_coin", lambda s: s)
    symbol = normalize(args.symbol)  # normalize: "ETH" → "hyna:ETH" etc.

    print(f"exchange : {args.exchange}")
    print(f"account  : {acc_cfg.name}")
    print(f"symbol   : {symbol}  (bot symbols: {', '.join(cfg.symbols)})")
    print(f"size     : ${args.size} USD")

    passed, failed = await smoke(client, symbol, args.size)

    print(f"\n{'─' * 36}")
    total = passed + failed
    status = "all good" if not failed else f"{failed} FAILED"
    print(f"  {passed}/{total} passed  {status}")

    await client.http.close()
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
