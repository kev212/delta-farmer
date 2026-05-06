# Changelog

## v0.6.4 – 2026-05-06

### Fixes

- Fixed 01.xyz authentication to use the new Janus proxy login endpoint after the legacy authproxy was removed

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.6.3...v0.6.4

---

## v0.6.3 – 2026-05-01

### Fixes

- Fixed Omni `info` command crashing when the exchange removed the `company` field from the registration API
- Fixed 01.xyz points auto-discovery failing because the server action chunk was not included in RSC responses — now scans HTML page chunks as well

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.6.2...v0.6.3

---

## v0.6.2 – 2026-04-05

### Fixes

- Fixed 01.xyz points auth breaking after a deployment change; auto-discovery now updates the action hash and deployment ID without manual intervention
- Fixed 01.xyz points pagination so all records are fetched instead of only the first page
- Fixed 01.xyz fee rates not being applied correctly

### Improvements

- Git commit hash is now shown in the version line of `info` commands

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.6.1...v0.6.2

---

## v0.6.1 – 2026-04-05

### Fixes

- Fixed Nado balance to include all spot assets (stables, NLP, etc) instead of only partial balances
- Fixed Nado PnL reporting using mismatched account lists before and after the trade
- Increased Nado isolated margin buffer to prevent undercollateralised orders

### Improvements

- Resting limit orders are now kept alive while BBO remains stable instead of being replaced
- Added exponential backoff on cycle failure with a configurable `max_failures` option to restore previous behaviour
- Improved private key error messages to be human-readable

**Full Changelog**: https://github.com/vladkens/delta-farmer/compare/v0.6.0...v0.6.1

---

## v0.6.0 — 2026-03-29

### New

- **01.xyz** — points balance, rank, and trade history now shown in `info` and `stats`
- **Hyena and Onyx stats** — Onyx now has a `stats` command; fills are cached locally on both exchanges
- **Weekly report script** — `scripts/weekly.py` aggregates volume, burn, and points across all exchanges from local cache

### Fixes

- Nado: balance now correctly sums USDT + USDC deposits
- Nado: trade close PnL was wrong due to Nado keeping closed positions as zero-size entries temporarily
- Nado: margin check was too conservative, causing false "insufficient margin" rejections
- Omni: switched to leaderboard v2 — points rank now visible in `info`

[v0.5.1...v0.6.0](https://github.com/vladkens/delta-farmer/compare/v0.5.1...v0.6.0)

---

## v0.5.1 — 2026-03-22

### Fixes

- Hyena and Onyx no longer pick up positions from unrelated DEXs sharing the same underlying HL account
- Hyena: bare symbols like `BTC` are now automatically prefixed with `hyna:` — no config change needed

[v0.5.0...v0.5.1](https://github.com/vladkens/delta-farmer/compare/v0.5.0...v0.5.1)

---

## v0.5.0 — 2026-03-21

### New

- **Hyena, Onyx, 01.xyz** — three new exchanges. Hyena and Onyx are built on HyperLiquid. 01.xyz is a Solana based perpdex accessible with an EVM wallet.
- **Balance-based trade sizing** — new `trade_size_pct` option sizes trades as a fraction of account balance instead of a fixed USD amount. The tightest account sets the constraint for the whole group.
- **Omni limit orders** — Omni now supports limit orders for the prime account (`use_limit = true`).
- **`config new` command** — generates a ready-to-edit config file for any exchange.
- **`positions` command** — shows open positions with margin and ROI metrics.

### Fixes

- Nado: limit orders were canceled too early on partial fills
- Positions are now emergency-closed if actual size deviates from expected (liquidation detection)

[v0.4.1...v0.5.0](https://github.com/vladkens/delta-farmer/compare/v0.4.1...v0.5.0)

---

## v0.4.1 — 2026-03-18

### New

- Nado: isolated symbols support
- Omni: referral code shown in `info`; account balances included in trade-stop Telegram notifications
- Stats tables now show date ranges in period labels and use a unified layout across all exchanges

### Fixes

- Nado: incorrect period labels in stats
- Stats table crash on certain data shapes

[v0.4.0...v0.4.1](https://github.com/vladkens/delta-farmer/compare/v0.4.0...v0.4.1)

---

## v0.4.0 — 2026-03-12

### New

- **Multi-symbol basket mode** — one trade cycle can now cover 2–4 symbols simultaneously. Each symbol stays delta-neutral, and each account also nets out across the full basket. Configure with `symbols_per_trade = 2` (or 3, 4).
- **Telegram notifications** — get push alerts when a trade opens/closes, on errors and crashes, and periodic digests with volume and burn stats. Add a `[telegram]` section to your config to enable.
- **Combined basket ROI limit** — new `combined_roi_limit` safety check closes the full basket if total P&L across all positions exceeds the threshold, in addition to the existing per-position check.

### Fixes

- Ethereal: fixed points count and authentication on the points endpoint
- Nado: fixed gap between live and archive trade data
- Pacifica: fixed balance display in the `info` command
- Limit order polling reliability improved across all exchanges

---

## v0.3.0 — 2026-03-04

### New

- **Ethereal support** — full trading, stats, and points tracking for Ethereal (EVM).
- **Nado support** — full trading, stats, and points tracking for Nado (EVM).
- **Grouped trading mode** — split accounts into independent strategy groups running in parallel within one process. Configure with `group_size` and optionally `regroup_interval` to periodically reshuffle groups.

---

## v0.2.0 — 2026-02-21

### New

- **Omni support** — full trading and stats for Omni (EVM, by Variational).
- **Stats filtering by day** — `stats -g day` groups results by day instead of week.
- Pacifica genesis date corrected to match the official UI and Discord.

---

## v0.1.0 — 2026-02-14

Initial release with **Pacifica** (Solana) support — delta-neutral trading, multi-account management, encrypted key storage, limit/market order modes, and weekly stats.
