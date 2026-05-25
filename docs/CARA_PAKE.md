# Cara Pake Delta Farmer — Omni

## Setup Awal (Sekali doang)

```bash
# 1. Buka Terminal, masuk folder
cd /Users/khezuma/workspace/delta-farmer

# 2. Source env
source $HOME/.local/bin/env

# 3. Isi privkey di config
# Buka file configs/omni.toml pake editor
# Ganti "your-private-key-here" dengan privkey wallet lo
#   omni-a = wallet 1 (isi disini)
#   omni-b = wallet 2 (isi disini)

# 4. Encrypt config (biar aman)
uv run apps/omni.py config encrypt -c configs/omni.toml

# 5. Cek koneksi & balance
uv run apps/omni.py info -c configs/omni.toml

# 6. Sample spread (5 menit — buat tuning threshold)
uv run tools/spread_sampler.py --exchange omni --symbols BTC,ETH,SOL,HYPE --duration 300
```

---

## Perintah Harian

### Cek Balance & Progress

```bash
cd /Users/khezuma/workspace/delta-farmer
source $HOME/.local/bin/env
uv run apps/omni.py info -c configs/omni.toml
```

Output:
```
✓  omni-a  0xabc..123  Volume: 5000  Balance: $49.50
✓  omni-b  0x456..789  Volume: 5000  Balance: $50.30
```

### Cek Posisi Open (kalau ada)

```bash
uv run apps/omni.py positions -c configs/omni.toml
```

### Cek Stats Period

```bash
uv run apps/omni.py stats -c configs/omni.toml last
uv run apps/omni.py stats -c configs/omni.toml --force
```

---

## Trading

### Manual (Lo tunggu & lihat)

```bash
cd /Users/khezuma/workspace/delta-farmer
source $HOME/.local/bin/env
uv run apps/omni.py trade -c configs/omni.toml
```

Bot jalan. Kalo mau stop: `Ctrl+C`

### Production (24/7 via tmux)

```bash
cd /Users/khezuma/workspace/delta-farmer
source $HOME/.local/bin/env
tmux new -s omni
uv run apps/omni.py trade -c configs/omni.toml
```

Setelah bot jalan:
- **Detach** (biarkan jalan di background): `Ctrl+B` trus lepas, tekan `D`
- **Attach** (lihat log): `tmux attach -t omni`
- **Stop bot**: attach dulu, terus `Ctrl+C`

---

## Emergency

### Close Semua Posisi Sekarang

```bash
cd /Users/khezuma/workspace/delta-farmer
source $HOME/.local/bin/env
uv run apps/omni.py close -c configs/omni.toml
```

---

## Setting Penting di Config

File: `configs/omni.toml`

| Parameter | Value skrg | Fungsi |
|---|---|---|
| `leverage` | 10 | Besar kecil modal dipake |
| `trade_size_usd` | 150-180 | $ per cycle |
| `trade_duration` | 20-25m | Lama hold posisi |
| `trade_cooldown` | 3-5m | Istirahat antar cycle |
| `market_slippage_open` | 0.003 (0.3%) | Max slip pas buka posisi |
| `market_slippage_close` | 0.001 (0.1%) | Max slip pas tutup posisi |
| `max_spread_open_bps` | 5 | Stop buka kalau spread > 5 bps |
| `max_spread_close_bps` | 10 | Tunggu kalau spread > 10 bps |
| `max_delta_pnl_pct` | 0.005 (0.5%) | Cek PnL seimbang sebelum close |
| `limit_drift_pct` | 0.001 (0.1%) | Seberapa jauh harga boleh gerak sebelum fallback market |
| `use_limit` | true | Prime wallet maker (irit fee) |
| `first_as_prime` | false | Random siapa jadi prime tiap cycle |

---

## Estimasi

**Dengan setting skrg:**
- 1 cycle = ~30 menit
- 1 hari = ~45-50 cycle
- Volume/hari = ~$15-17K
- Volume/minggu = ~$100-120K
- Cost/hari = ~$1-3 (1-3% dari modal $100)

---

## Catatan

- **Jangan share configs/omni.toml** (isinya privkey walaupun udah encrypt)
- **Jangan commit configs/omni.toml** ke git (udah auto-ignore)
- **Pakai wallet baru** khusus farming (bukan main wallet)
- Kalau error: cek balance, cek registered, cek network
- Masalah? Buka lagi terminal, run `info`, liat balance
