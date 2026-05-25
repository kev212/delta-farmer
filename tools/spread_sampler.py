# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Observe live BBO spread for threshold tuning

import argparse
import asyncio
import statistics
import time
from decimal import Decimal

from clients.nado import NadoClient
from clients.omni import OmniClient
from lib.cli import run_app
from strategy import StrategyConfig

EXCHANGES = {"nado": NadoClient, "omni": OmniClient}


async def sample(client, symbols: list[str], duration: int, interval: int):
    samples: dict[str, list[Decimal]] = {s: [] for s in symbols}
    started = time.time()

    print(f"Sampling {client.exchange} for {duration}s, every {interval}s...")
    print(f"Symbols: {', '.join(symbols)}\n")

    await client.warmup()

    n = 0
    while time.time() - started < duration:
        for sym in symbols:
            try:
                bid, ask = await client.get_bbo(sym)
                mid = (bid + ask) / 2
                bps = ((ask - bid) / mid) * Decimal(10000)
                samples[sym].append(bps)
                print(f"  [{n:3d}] {sym}: bid={bid:.4f} ask={ask:.4f} spread={bps:.2f}bps")
            except Exception as e:
                print(f"  [{n:3d}] {sym}: ERROR {type(e).__name__}: {e}")
        n += 1
        await asyncio.sleep(interval)

    print("\n" + "=" * 60)
    print(f"Summary ({n} samples per symbol):\n")
    for sym, vals in samples.items():
        if not vals:
            print(f"  {sym}: no data")
            continue
        floats = [float(v) for v in vals]
        floats.sort()
        n_v = len(floats)

        def _percentile(q: float) -> float:
            return floats[int(n_v * q)] if n_v else 0

        p25 = _percentile(0.25)
        p50 = _percentile(0.5)
        p75 = _percentile(0.75)
        p95 = _percentile(0.95)
        print(
            f"  {sym}:  min={floats[0]:.1f}  p25={p25:.1f}  p50={p50:.1f}  "
            f"p75={p75:.1f}  p95={p95:.1f}  max={floats[-1]:.1f}  "
            f"mean={statistics.mean(floats):.1f} bps"
        )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", choices=list(EXCHANGES), required=True)
    ap.add_argument("--config", default=None, help="Config path (default: configs/<exchange>.toml)")
    ap.add_argument("--symbols", required=True, help="Comma-separated, e.g. BTC,ETH")
    ap.add_argument("--duration", type=int, default=300, help="Total sampling duration in seconds")
    ap.add_argument("--interval", type=int, default=5, help="Sample interval in seconds")
    args = ap.parse_args()

    cfg_path = args.config or f"configs/{args.exchange}.toml"
    cfg = StrategyConfig.load(cfg_path)

    if not cfg.accounts:
        raise SystemExit("No accounts in config")
    acc_cfg = cfg.accounts[0]

    cls = EXCHANGES[args.exchange]
    client = cls.from_config(acc_cfg)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    await sample(client, symbols, args.duration, args.interval)


if __name__ == "__main__":
    run_app(main())
