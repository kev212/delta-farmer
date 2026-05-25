# delta-farmer

<p align="center"><img src=".github/logo.svg" width="200" /></p>

<div align="center">

[<img src="https://badges.ws/badge/-/%40uid127/000?icon=x&label" alt="x" />](https://x.com/uid127)
[<img src="https://badges.ws/badge/-/Telegram%20Channel/2CA5E0?icon=telegram&label" alt="tg channel" />](https://t.me/+nkSWfo2QASdiOTI0)
[<img src="https://badges.ws/badge/-/Telegram%20Chat/2CA5E0?icon=telegram&label" alt="tg chat" />](https://t.me/+JPqp0bteCWwzMDJk)

</div>

Automated delta-neutral trading for crypto points farming. Run classic two-sided hedges or balanced multi-symbol baskets across perpetual DEXs to maximize volume and points with limited directional risk.

- 🎯 **Delta-neutral by design** — matched long/short positions minimize directional exposure
- 🧩 **Multi-symbol basket mode** — trade 2–4 symbols in one cycle, each leg staying neutral
- 🔄 **Multi-account management** — one config file drives all your accounts simultaneously
- 👥 **Grouped trading** — split accounts into independent strategy groups
- 📊 **Real-time safety checks** — emergency close if ROI limits are breached
- 🔐 **Encrypted key storage** — private keys never sit in plaintext
- 📨 **Telegram notifications** — get alerts on trade start, stop, errors, and periodic reports
- 🎲 **Configurable sizing and timing** — randomized sizes and durations to vary on-chain patterns
- 🛡️ **Slippage & spread guardrails** — configurable slippage tolerance and auto-skip cycles when spreads are too wide
- ⚖️ **Delta-PnL safety gate** — holds close until spread normalizes and PnL rebalances, preventing bad exits

---

## Differences from OG ([vladkens/delta-farmer](https://github.com/vladkens/delta-farmer))

This fork adds three layers of spread and slippage protection not present in the original:

### 1. Configurable slippage (bypasses hardcoded defaults)
The original hardcodes slippage at 0.5% open / 0.1% close in each client. This fork exposes them as config parameters:

```toml
market_slippage_open = 0.003      # 0.3% instead of default 0.5%
market_slippage_close = 0.001     # 0.1% (same as default)
```

Passed through all 7 exchange clients (`Nado`, `Omni`, `Ethereal`, `Hyena`, `HyperLiquid`, `Pacifica`, `01.xyz`).

### 2. Spread guardrail (auto-skip bad fills)
Before opening any position, the bot checks BBO spread on every leg. If any leg exceeds `max_spread_open_bps`, the entire cycle is skipped:

```toml
max_spread_open_bps = 15   # skip if spread > 15bps at open
max_spread_close_bps = 30  # wait if spread > 30bps before close
```

Includes `tools/spread_sampler.py` — a CLI utility to observe real BBO spreads and pick realistic thresholds.

### 3. Delta-PnL pre-close gate (prevent bad exits)
Before closing, the bot checks that the combined PnL across both legs is near zero (as expected in delta-neutral). If spread is wide or PnL is imbalanced, it waits up to `close_safety_wait_sec` for conditions to normalize before force-closing:

```toml
max_delta_pnl_pct = 0.005         # 0.5% of total notional max imbalance
close_safety_wait_sec = 300       # max 5 minute wait
close_safety_poll_sec = 15        # re-check every 15s
```

### Other additions
- `docs/SETUP_GUIDE.md` — full setup walkthrough for single and dual-instance farming
- Tightened default safety limits (`position_roi_limit: 0.5`, `combined_roi_limit: 0.05`, `max_failures: 5`) in example configs
- All changes are backward-compatible — existing configs work without modification

---

## What is delta-farmer?

Delta-farmer is a trading bot that automatically opens matched long and short positions on perpetual DEXs. The idea is simple: by holding equal opposite-side trades, your net market exposure stays near zero — you're farming trading volume and protocol points rather than betting on price direction.

Each trading cycle, the bot:

1. Opens a **long** position on one account and a **short** on another (or splits across multiple assets)
2. Holds them for a configurable duration while monitoring risk
3. Closes everything cleanly and waits before the next cycle
4. Sends you a Telegram summary if configured

You control the size, timing, leverage, and which exchange to run on. The bot handles the rest.

---

## Supported Exchanges

| Name     | Network | Link                                          | Referral                                                           |
| -------- | ------- | --------------------------------------------- | ------------------------------------------------------------------ |
| Ethereal | EVM     | [ethereal.trade](https://app.ethereal.trade/) | [Sign up](https://app.ethereal.trade/?ref=DSQ3BOJ65L3X)            |
| HyENA    | EVM     | [hyena.trade](https://app.hyena.trade/)       | [Sign up](https://app.hyena.trade/ref/VLADKENS)                    |
| Nado     | EVM     | [nado.xyz](https://app.nado.xyz/)             | [Sign up](https://app.nado.xyz?join=yUAjz7a)                       |
| Omni     | EVM     | [variational.io](https://omni.variational.io) | [Sign up](https://omni.variational.io)                             |
| Onyx     | EVM     | [onyx.live](https://app.onyx.live/)           | [Sign up](https://app.onyx.live/?ref=BB7M4BW3)                     |
| Pacifica | Solana  | [pacifica.fi](https://app.pacifica.fi)        | [Sign up](https://app.pacifica.fi?referral=uid127)                 |
| 01.xyz   | EVM     | [01.xyz](https://01.xyz/)                     | [Sign up](https://01.xyz/ref/019d07db-c7ef-757e-80a4-8a40213488d2) |

---

## Installation

### Step 1 — Install prerequisites

#### macOS

Open **Terminal** (`Cmd + Space` → type "Terminal" → Enter) and run:

```bash
xcode-select --install
```

A dialog will pop up — click "Install". Once done, install uv ([official guide](https://docs.astral.sh/uv/getting-started/installation/#__tabbed_1_1)):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close and reopen Terminal so the `uv` command becomes available.

#### Windows

Open **PowerShell** (`Win + S` → type "PowerShell" → Enter) and run:

```powershell
winget install --id Git.Git -e --source winget
```

Then install uv ([official guide](https://docs.astral.sh/uv/getting-started/installation/#__tabbed_1_2)):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen PowerShell so both `git` and `uv` become available.

### Step 2 — Download and run

```bash
git clone https://github.com/vladkens/delta-farmer.git
cd delta-farmer
```

That's it. Dependencies are installed automatically on the first run.

---

## Quick Start

Replace `<app>` with your exchange name throughout: `pacifica`, `omni`, `ethereal`, `nado`, `hyena`, `onyx`, or `zero1`.

**Step 1 — Create a config file**

```bash
uv run apps/<app>.py config new
```

This creates `configs/<app>.toml` pre-filled with sensible defaults. Open the file in any text editor.

**Step 2 — Add your private keys**

Find the `[[accounts]]` sections and paste your private keys:

```toml
[[accounts]]
name = "acc1"
privkey = "your-private-key-here"

[[accounts]]
name = "acc2"
privkey = "your-private-key-here"
```

You need at least **2 accounts** — one goes long, the other goes short.

**Step 3 — Encrypt your keys**

```bash
uv run apps/<app>.py config encrypt
```

You'll be prompted for a password. After this step, the raw keys are replaced with encrypted values in the file. You'll enter this password each time you start the bot (or set it in `.env` — see [Password Management](#password-management)).

**Step 4 — Start trading**

```bash
uv run apps/<app>.py trade
```

### Detailed Setup Guide

For a complete walkthrough covering wallet setup, spread sampling, burn rate optimization, and production deployment, see [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md).

---

## Commands

All exchanges share the same command structure. Replace `<app>` with your exchange name.

```bash
# Trading
uv run apps/<app>.py trade          # Start automated trading
uv run apps/<app>.py close          # Close all open positions
uv run apps/<app>.py info           # View account balances & points
uv run apps/<app>.py positions      # View current open positions

# Statistics
uv run apps/<app>.py stats          # Current period stats (cached 1h)
uv run apps/<app>.py stats last     # Previous period only
uv run apps/<app>.py stats --force  # Force-refresh cached stats
uv run apps/<app>.py clean          # Delete all cached data

# Config management
uv run apps/<app>.py config new            # Create a new config file
uv run apps/<app>.py config new -c my.toml # Create at a custom path
uv run apps/<app>.py config encrypt        # Encrypt private keys in config
uv run apps/<app>.py config decrypt        # Decrypt to view raw keys

# Help
uv run apps/<app>.py --help

# Spread sampler (tune guardrail thresholds)
uv run tools/spread_sampler.py --exchange nado --symbols BTC,ETH --duration 600
```

---

## Configuration Reference

All settings live in your `configs/<app>.toml` file. Here is every available parameter:

### Core settings

| Parameter           | Default  | Description                                                                                                                                              |
| ------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `leverage`          | `10`     | Leverage multiplier (1–49). Set it to the **lowest** max leverage across all your chosen symbols.                                                        |
| `symbols`           | required | Trading pairs, e.g. `["BTC"]` or `["BTC", "ETH"]`. Check the exchange UI for available symbols.                                                          |
| `symbols_per_trade` | `1`      | How many symbols to trade per cycle. `1` = classic mode; `2`–`4` = basket mode. Must match the length of `symbols`.                                      |
| `use_limit`         | `false`  | If `true`, the prime account opens with a limit order instead of a market order — reduces fees.                                                          |
| `first_as_prime`    | `false`  | If `true`, the first account in the list is always the prime (limit-side). If `false`, it rotates randomly each cycle. Ignored when `group_size` is set. |

### Trade sizing

Exactly one of these is required — you cannot use both.

| Parameter        | Default | Description                                                                                                         |
| ---------------- | ------- | ------------------------------------------------------------------------------------------------------------------- |
| `trade_size_usd` | —       | Total notional per cycle in USD, as a range: `{ min = 140, max = 160 }`. The amount is split 50% prime / 50% hedge. |
| `trade_size_pct` | —       | Size as a fraction of account balance (e.g. `0.5` = 50%). The tightest account sets the binding constraint.         |

### Timing

Durations accept seconds (`30`), strings like `"15s"`, `"5m"`, `"1h"`, or a range `{ min = "15m", max = "20m" }`.

| Parameter         | Default  | Description                                          |
| ----------------- | -------- | ---------------------------------------------------- |
| `trade_duration`  | required | How long to hold positions each cycle.               |
| `trade_cooldown`  | required | Pause between cycles.                                |
| `trade_heartbeat` | `"15s"`  | How often safety checks run while holding positions. |

### Limit order settings

Only relevant when `use_limit = true`.

| Parameter               | Default | Description                                                                                           |
| ----------------------- | ------- | ----------------------------------------------------------------------------------------------------- |
| `limit_wait`            | `"90s"` | How long to wait for a limit order to fill.                                                           |
| `limit_market_fallback` | `true`  | If the limit order times out, fall back to a market order. Set to `false` to abort the cycle instead. |

### Safety limits

| Parameter            | Default | Description                                                                                                                                                 |
| -------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `position_roi_limit` | `0.8`   | Emergency-close the full cycle if any single position reaches ±80% ROI.                                                                                     |
| `combined_roi_limit` | `0.1`   | Emergency-close if the combined basket ROI reaches ±10%.                                                                                                    |
| `max_failures`       | `0`     | Stop the strategy after this many consecutive cycle failures. `0` = never stop — retries indefinitely with exponential backoff (up to 1h between attempts). |

### Slippage tolerance

Controls how much slippage to allow on market orders. Overrides hardcoded exchange defaults.

| Parameter                 | Default | Description                                                                                 |
| ------------------------- | ------- | ------------------------------------------------------------------------------------------- |
| `market_slippage_open`    | `0.005` | Max slippage when opening positions (0.5%). Range: 0–0.05 (0–5%).                           |
| `market_slippage_close`   | `0.001` | Max slippage when closing positions (0.1%). Range: 0–0.05 (0–5%).                           |

### Spread guardrail

Skip the cycle if BBO spread exceeds the threshold. Useful for avoiding bad fills during volatile markets. Set to `0` to disable.

| Parameter              | Default | Description                                                       |
| ---------------------- | ------- | ----------------------------------------------------------------- |
| `max_spread_open_bps`  | `0`     | Skip cycle if any leg's spread > this value (in bps) at open.     |
| `max_spread_close_bps` | `0`     | Wait for spread to normalize before closing (in bps).             |

### Delta-PnL gate (close-time safety)

Before closing, the bot checks that the total combined PnL is balanced (near zero as expected in delta-neutral). If spread or PnL deviates, it waits for conditions to improve before force-closing.

| Parameter                | Default | Description                                                   |
| ------------------------ | ------- | ------------------------------------------------------------- |
| `max_delta_pnl_pct`      | `0`     | Max allowed delta PnL as fraction of notional (`0.005` = 0.5%). `0` = disabled. |
| `close_safety_wait_sec`  | `300`   | Max wait before force-closing (seconds).                      |
| `close_safety_poll_sec`  | `15`    | How often to re-check conditions (seconds).                   |

### Grouped trading

| Parameter          | Default | Description                                                                                                   |
| ------------------ | ------- | ------------------------------------------------------------------------------------------------------------- |
| `group_size`       | —       | Split accounts into independent groups. Must be 2–5. Total enabled accounts must be divisible by this number. |
| `regroup_interval` | —       | Re-sort accounts by balance and restart groups on this interval. Only active when `group_size` is set.        |

### Accounts

Add one `[[accounts]]` block per wallet.

| Parameter | Default  | Description                                                                    |
| --------- | -------- | ------------------------------------------------------------------------------ |
| `name`    | required | Display name shown in logs and stats.                                          |
| `privkey` | required | Private key. Fill it in, then run `config encrypt`.                            |
| `proxy`   | —        | Optional HTTP proxy: `"http://user:pass@host:port"`.                           |
| `enabled` | `true`   | Set to `false` to exclude this account from trading while keeping it in stats. |

### Telegram (optional)

Add a `[telegram]` block to enable notifications.

| Parameter         | Default      | Description                                                                                                        |
| ----------------- | ------------ | ------------------------------------------------------------------------------------------------------------------ |
| `token`           | —            | Bot token from [@BotFather](https://t.me/BotFather). Run `config encrypt` after adding it.                         |
| `chat_id`         | —            | Your personal or group chat ID. Get it from [@userinfobot](https://t.me/userinfobot).                              |
| `notify`          | all channels | List of notification channels to enable. Remove any to silence them: `"start"`, `"stop"`, `"errors"`, `"reports"`. |
| `report_interval` | `"1h"`       | How often to send a periodic stats digest.                                                                         |

---

## Trading Modes

### Classic mode (single symbol)

One cycle trades one symbol: one account goes long, the other goes short.

```toml
symbols = ["BTC"]
symbols_per_trade = 1
trade_size_usd = { min = 140, max = 160 }
```

### Basket mode (multi-symbol)

One cycle trades multiple symbols simultaneously. Each symbol stays neutral, and each account also nets out across the full basket.

```toml
symbols = ["BTC", "ETH"]
symbols_per_trade = 2
trade_size_usd = { min = 140, max = 160 }
```

Rules:

- `symbols_per_trade` must exactly match the number of entries in `symbols`
- Maximum 4 symbols per trade
- Safety exits apply both per-position and combined basket ROI

### Grouped trading

Splits your accounts into independent strategy groups that run in parallel within one process.

```toml
group_size = 2
regroup_interval = "12h"
```

Rules:

- `group_size` must be between 2 and 5
- Total enabled account count must divide evenly by `group_size`
- `first_as_prime` is ignored when `group_size` is set
- `regroup_interval` re-balances groups by account balance and restarts them

---

## Safety Checks

### Runtime checks (every heartbeat)

`trade_heartbeat` interval (default 15 seconds), the bot checks:

1. **Per-position ROI** — if any single leg's return crosses `±position_roi_limit` (default ±80%), all positions are closed immediately
2. **Combined basket ROI** — if the total basket return crosses `±combined_roi_limit` (default ±10%), all positions are closed immediately
3. **Position count** — if any symbol has an unexpected number of positions (e.g. one side was liquidated), all positions are closed immediately

### Pre-open spread guardrail

Before opening, the bot checks BBO spread on every leg. If any leg exceeds `max_spread_open_bps`, the entire cycle is skipped — avoiding bad fills in thin markets.

### Pre-close safety gate

Before closing, the bot waits for two conditions to clear:

1. **Spread** — all legs' BBO spread must be below `max_spread_close_bps`
2. **Delta PnL** — total combined PnL must stay within `max_delta_pnl_pct` of notional (i.e., the delta-neutral hedge is still balanced)

If conditions don't improve within `close_safety_wait_sec`, the bot force-closes anyway (holding longer is riskier). This prevents bad exits when prices are volatile.

### Spread sampler

Use the built-in sampler to observe real BBO spreads before setting thresholds:

```bash
uv run tools/spread_sampler.py --exchange omni --symbols BTC,ETH,SOL,HYPE --duration 600
```

Output example:
```
  BTC:  min=2.1  p25=4.3  p50=5.8  p75=10.1  p95=22.0  mean=7.2 bps
```

Set `max_spread_open_bps` based on the p75 value plus a buffer (e.g., p75=10 → set 15).

These are last-resort protections. You should also use sensible leverage and trade sizes.

---

## Telegram Notifications

**Setup:**

1. Message [@BotFather](https://t.me/BotFather) on Telegram, create a bot, copy the token
2. Message [@userinfobot](https://t.me/userinfobot) to get your chat ID
3. Add to your config:

```toml
[telegram]
token = "123456:ABC-DEF..."
chat_id = "123456789"
notify = ["start", "stop", "errors", "reports"]
report_interval = "1h"
```

4. Encrypt the token: `uv run apps/<app>.py config encrypt`
5. Test it: `uv run apps/<app>.py tgtest`

**Notification channels:**

| Channel   | When it fires                                  |
| --------- | ---------------------------------------------- |
| `start`   | A trade cycle opens (symbol, size, accounts)   |
| `stop`    | A trade cycle closes (PnL, duration)           |
| `errors`  | Cycle failures and crashes                     |
| `reports` | Periodic digest (trades, volume, burn, $/100k) |

Remove a channel from the `notify` list to silence it.

---

## Private Key Encryption & Passwords

Private keys in your config are encrypted using AES. After filling in raw keys, always run:

```bash
uv run apps/<app>.py config encrypt
```

The bot prompts for your password on startup. To skip the prompt, save the password in a `.env` file in the project folder:

```bash
echo "DF_CONFIG_PASSWORD=your-password-here" >> .env
```

To view raw keys again (for backup or migration):

```bash
uv run apps/<app>.py config decrypt
```

---

## Running Multiple Instances / Custom Configs

Use the `-c` flag to point to a different config file:

```bash
uv run apps/pacifica.py -c configs/pacifica-set2.toml trade
```

This lets you run multiple independent instances of the same exchange with different accounts or settings:

```bash
# Terminal 1
uv run apps/omni.py -c configs/omni-set1.toml trade

# Terminal 2
uv run apps/omni.py -c configs/omni-set2.toml trade
```

---

## Updating

```bash
# Stop running instances (Ctrl+C or kill the process)

# Pull latest changes
git pull

# Update dependencies
uv sync

# Restart trading
uv run apps/<app>.py trade
```

---

## Recommended Services

- [**Digital Ocean**](https://m.do.co/c/a97fd963258f) — VPS for running the bot 24/7 in the background
- [**Proxy Shard**](https://proxyshard.com?ref=5406) — proxies for separating account traffic

---

## Telemetry

Delta-farmer collects anonymous usage statistics (exchange name, command used, technical config flags) to understand adoption and popular features. No wallet addresses, balances, or strategy parameters are ever sent.

Set `DF_TELEMETRY=0` to opt out completely.

---

## Weekly Report

Sync stats for all exchanges first, then run the report:

```bash
uv run apps/<app>.py stats --force   # refresh each exchange

uv run scripts/weekly.py             # all exchanges, latest week
uv run scripts/weekly.py -1          # one week back
uv run scripts/weekly.py -e Hyena    # one exchange, all periods
uv run scripts/weekly.py --burn      # burn only, all exchanges over time
```

---

## Known Issues

### SSL certificate error on macOS

```
urllib.error.URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]>
```

Caused by a bug in `curl-cffi` v0.14. Fix:

```bash
uv add curl-cffi==0.15.0b4 --prerelease allow && uv sync
```

---

## Risk Disclaimer

**USE AT YOUR OWN RISK**

- This software is for educational purposes only
- Trading cryptocurrencies carries significant financial risk
- You may lose all deposited funds
- No guarantees of profit or airdrop eligibility
- Always test with small amounts first
- The authors are not responsible for any losses

---

## Contact & Feedback

- **X/Twitter:** [@uid127](https://x.com/uid127)
- **Telegram channel:** [@eazyrekt](https://t.me/s/eazyrekt) — drop farming insights & updates
- **Telegram chat:** [Join the group](https://t.me/+JPqp0bteCWwzMDJk)
