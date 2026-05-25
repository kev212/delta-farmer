# Setup Guide — Delta Farmer

## Overview

Delta-neutral farming bot untuk perp DEX points. Strategi: long + short di exchange yang sama (same-exchange multi-wallet hedge).

## Dua Setup

### Single Instance (Modal Kecil, $100)
Satu exchange, 2 wallet. Fokus Omni karena free trading fee. Rekomendasi awal.

### Dual Instance (Modal Cukup, $500+)
Dua exchange, masing-masing 2 wallet. Omni + Nado jalan parallel.

---

## Pre-Flight Checklist

### Wallet & Funding

- [ ] 2 wallet baru per exchange (jangan main wallet!)
- [ ] Deposit per wallet: $50 (Omni), $75+ (Nado)
- [ ] ETH buat gas (Nado via Arbitrum)
- [ ] Register di exchange via referral link dari README

### Tools

- [ ] Python 3.14 (`uv python list`)
- [ ] Git installed
- [ ] Terminal multiplexer (`tmux` atau `screen`)

---

## Single Instance: Omni Only ($100 Capital)

### Step 1 — Isi Privkey

Edit `configs/omni.toml`, isi `privkey` kedua akun:

```toml
[[accounts]]
name = "omni-a"
privkey = "0xabcdef..."  # wallet 1 private key

[[accounts]]
name = "omni-b"
privkey = "0x123456..."  # wallet 2 private key
```

### Step 2 — Encrypt

```bash
uv run apps/omni.py config encrypt -c configs/omni.toml
```

Lo akan diminta password. Simpan password (buat startup). Bisa di-skip dengan `DF_CONFIG_PASSWORD` env var.

### Step 3 — Verify Account & Balance

```bash
uv run apps/omni.py info -c configs/omni.toml
```

Output yang diharapkan:
```
✓  omni-a  0xabc..def  Volume: 0  Points: 0  Balance: $50.00
✓  omni-b  0x123..456  Volume: 0  Points: 0  Balance: $50.00
```

### Step 4 — Sample Spread (Tuning Threshold)

Jalankan 5-10 menit buat observasi spread Omni:

```bash
uv run tools/spread_sampler.py --exchange omni --symbols BTC,ETH,SOL,HYPE --duration 600
```

Ambil p75 dari tiap symbol, set `max_spread_open_bps` sedikit di atasnya (p75 + 5 bps).

Contoh output:
```
  BTC: min=2.1  p25=4.3  p50=5.8  p75=10.1  p95=22.0  mean=7.2 bps
  ETH: min=3.5  p25=5.8  p50=8.2  p75=15.4  p95=30.1  mean=9.8 bps
  SOL: min=8.0  p25=12.0  p50=18.5  p75=28.0  p95=45.0  mean=21.0 bps
  HYPE: min=12.0  p25=20.0  p50=32.0  p75=50.0  p95=85.0  mean=38.0 bps
```

Maka set `max_spread_open_bps = 55` (covers HYPE p75 = 50 + 5 buffer).

### Step 5 — First Cycle (Manual)

```bash
uv run apps/omni.py trade -c configs/omni.toml
```

Biarkan 1 cycle penuh (open → hold ~20m → close). Setelah close, cek balance:

```bash
uv run apps/omni.py info -c configs/omni.toml
```

### Step 6 — Production Run

**Via tmux (recommended):**
```bash
tmux new -s omni
uv run apps/omni.py trade -c configs/omni.toml
# Ctrl+B, D — detach
```

**Re-attach:**
```bash
tmux attach -t omni
```

**Stop bot:**
```bash
tmux attach -t omni  # or just kill process
Ctrl+C                # graceful close positions
```

---

## Burn Rate Calculator

### Formula

```
Cost per cycle = trade_size × 2 legs × (slippage_open + slippage_close)
Daily cost = cost per cycle × cycles_per_day
Daily burn % = daily_cost / total_capital × 100

Volume per cycle = trade_size × 2 legs
Daily volume = volume per cycle × cycles_per_day
Weekly volume = daily volume × 7
```

### Reference Table ($100 capital, leverage 10x)

| Trade Size | Cycle/ Hari | Slippage | Daily Cost | Daily Vol | Weekly Vol | Burn/Day |
|------------|------------|----------|------------|-----------|------------|----------|
| $150       | 55         | 0.3%      | $2.48      | $16.5K   | $115K      | 2.5%     |
| $165       | 55         | 0.2%      | $1.82      | $18.2K   | $127K      | 1.8%     |
| $165       | 55         | 0.3%      | $2.72      | $18.2K   | $127K      | 2.7%     |
| $200       | 55         | 0.3%      | $3.30      | $22.0K   | $154K      | 3.3%     |
| $200       | 70         | 0.3%      | $4.20      | $28.0K   | $196K      | 4.2%     |

### Real Cost vs Slip Setting

| `market_slippage_open` | `market_slippage_close` | effective slip/leg | note |
|------------------------|------------------------|--------------------|------|
| 0.005 (default)        | 0.001 (default)        | ~0.5%              | default upstream — terlalu mahal |
| 0.003                  | 0.001                  | ~0.3%              | moderate |
| 0.002                  | 0.001                  | ~0.2%              | tight — resiko order reject |
| 0.002                  | 0.0005                 | ~0.15%             | super tight — Omni RFQ mungkin tolak |

---

## Sizing Decision Tree

```
Berapa modal total?
  ├─ <$200 → start dengan Omni only, trade_size $150-200
  ├─ $200-500 → Omni dulu, scale up trade_size
  └─ $500+ → dual instance (Omni + Nado parallel)

Slippage tolerance?
  ├─ Conservative (0.2%) → trade_size lebih besar (hold lebih singkat)
  └─ Moderate (0.3%) → balance, default setting

Target weekly volume?
  ├─ 75-100K → trade_size $165, 55 cycle/day (setting default)
  ├─ 150K+ → trade_size $250+, but requires burn >5% 
  └─ <50K → lebih konservatif, hold lebih panjang
```

---

## Dual Instance Setup ($500+ Modal)

### Concept

```
Terminal 1: uv run apps/omni.py trade -c configs/omni.toml
  └─ Wallet A long ↔ Wallet B short (Omni)

Terminal 2: uv run apps/nado.py trade -c configs/nado.toml
  └─ Wallet C long ↔ Wallet D short (Nado)
```

### Capital Allocation

| Exchange | Wallet | Capital |
|----------|--------|---------|
| Omni | omni-a | $125 |
| Omni | omni-b | $125 |
| Nado | nado-a | $125 |
| Nado | nado-b | $125 |
| **Total** | | **$500** |

### Setup Steps

Sama seperti single instance, tapi diulang 2 kali (Omni + Nado).

1. Isi privkey di kedua config
2. Encrypt keduanya
3. Verify balance masing-masing
4. Sample spread per exchange
5. Buat 2 tmux session terpisah

### Script Helper (opsional)

```bash
# start-omni.sh
#!/bin/bash
cd /path/to/delta-farmer
source .venv/bin/activate
uv run apps/omni.py trade -c configs/omni.toml
```

---

## Production Monitoring

### Daily

```bash
# Cek balance
uv run apps/omni.py info -c configs/omni.toml

# Cek positions (kalau ada open)
uv run apps/omni.py positions -c configs/omni.toml
```

### Weekly

```bash
# Force-refresh stats
uv run apps/omni.py stats -c configs/omni.toml --force

# Lihat points per periode
uv run apps/omni.py stats -c configs/omni.toml last
```

### Telegram Notifications

Enable di config `[telegram]` section. Aktifin channel `"errors"` minimal — lo tau kalau ada yg crash.

---

## Troubleshooting

### "Not registered"

```bash
uv run apps/omni.py info -c configs/omni.toml
```

Kalau semua akun `✗`, lo belum register. Buka `https://omni.variational.io` dan deposit dulu.

### Cloudflare Error

Omni kadang kena Cloudflare. Bot sudah handle otomatis (warmup + retry 9x). Tapi first request bisa lambat 5-10 detik.

### Connection Error

Cek proxy kalau diatur. Kalau pake proxy, verifikasi `proxy` config: `"http://user:pass@host:port"`.

### "Account balance below min" Error

Trade size turun otomatis di bawah $10 (Omni min). Turunin `trade_size_usd` atau tambah deposit.

### Position size mismatch

Wajar terjadi karena lot rounding. delta-farmer handle via `positions_within_limits` (5% tolerance).

---

## Risk Reminders

1. **Privkey handling** — Encrypt config setelah isi privkey. Jangan share config file.
2. **Wallet hygiene** — Pakai wallet baru khusus farming. Laporkan kalau compromised.
3. **No guarantee profit** — Fungsi safety bukan jaminan profit. Bisa rugi.
4. **Exchange risk** — Smart contract risk, oracle failure, pause trading.
5. **Funding rate** — Same-exchange cancel out. Cross-exchange TIDAK cancel.
6. **Capital depletion** — Burn rate bisa exceed estimate kalau market volatile.

---

## Quick Reference

```bash
# Generate config baru
uv run apps/<exchange>.py config new -c configs/<name>.toml

# Encrypt privkey
uv run apps/<exchange>.py config encrypt -c configs/<name>.toml

# Cek info & balance
uv run apps/<exchange>.py info -c configs/<name>.toml

# Start trading
uv run apps/<exchange>.py trade -c configs/<name>.toml

# Close all positions (emergency)
uv run apps/<exchange>.py close -c configs/<name>.toml

# View open positions
uv run apps/<exchange>.py positions -c configs/<name>.toml

# Weekly stats
uv run apps/<exchange>.py stats -c configs/<name>.toml --force

# Sample spread
uv run tools/spread_sampler.py --exchange <exchange> --symbols BTC,ETH --duration 300
```
