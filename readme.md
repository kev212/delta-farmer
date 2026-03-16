# delta-farmer

<p align="center"><img src=".github/logo.svg" width="200" /></p>

Automated delta-neutral trading for crypto points farming. Run classic two-sided hedges or balanced multi-symbol baskets across perpetual DEXs to maximize volume and points with limited directional risk.

## Features

- 🎯 Delta-neutral trading strategies
- 🧩 Multi-symbol balanced trading mode
- 🔄 Multi-account position management
- 👥 Optional grouped trading mode
- 📊 Real-time ROI safety checks
- 🔐 Encrypted private key storage
- 🎲 Configurable trade sizes and timing
- 📨 Telegram push notifications

## Supported Protocols

| Protocol                                   | Tech     | Status      |
| ------------------------------------------ | -------- | ----------- |
| [Pacifica](https://pacifica.fi)            | Solana   | Ready       |
| [Omni](https://omni.variational.io)        | EVM      | Ready       |
| [Ethereal](https://app.ethereal.trade/)    | EVM      | Ready       |
| [Nado](https://app.nado.xyz/)              | EVM      | Ready       |
| [HyENA](https://app.hyena.trade/)          | EVM      | In Progress |
| [Extended](https://app.extended.exchange/) | Starknet | In Progress |

## Installation

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/getting-started/installation/#standalone-installer) package manager

### Setup

```bash
# Clone the repository
git clone https://github.com/vladkens/delta-farmer.git && cd delta-farmer

# Install dependencies
uv sync
```

## Quick Start

```bash
# Replace <app> with: pacifica, omni, ethereal, nado
cp configs.example/<app>.toml configs/<app>.toml
# Edit configs/<app>.toml with your private keys

uv run -m apps.<app> config encrypt  # Encrypt private keys
uv run -m apps.<app> trade           # Start trading
```

## Usage

All protocols share common commands:

```bash
# Replace <app> with: pacifica, omni, ethereal, nado

# Trading
uv run -m apps.<app> trade          # Start automated trading
uv run -m apps.<app> close          # Close all open positions
uv run -m apps.<app> info           # View account balances & points
uv run -m apps.<app> stats          # View trading statistics (cached 1h)
uv run -m apps.<app> stats last     # Show only previous period
uv run -m apps.<app> stats --force  # Force refresh cached data
uv run -m apps.<app> clean          # Delete cached data

# Config management
uv run -m apps.<app> config encrypt  # Encrypt private keys
uv run -m apps.<app> config decrypt  # Decrypt to view keys

# Help
uv run -m apps.<app> --help
```

### Using Custom Configs

Use the `-c` flag to specify different config files:

```bash
# Run with custom config
uv run -m apps.pacifica -c configs/pacifica-alt.toml trade

# Run multiple instances
uv run -m apps.omni -c configs/omni-set1.toml trade
uv run -m apps.omni -c configs/omni-set2.toml trade
```

### Grouped Trading Mode

You can run multiple independent strategy groups in one process:

```toml
group_size = 2
regroup_interval = "12h"
```

Rules:

- `group_size` must be between `2` and `5`
- if `group_size` is not set, single-group mode supports at most `5` enabled accounts
- if `group_size` is set, enabled account count must be divisible by `group_size`
- when `group_size` is set, `first_as_prime` is ignored
- `regroup_interval` is applied only when `group_size` is set

### Multi-Symbol Trading Mode

The new basket mode lets one cycle trade multiple symbols at once while staying neutral in two ways:

- each symbol is opened with equal long and short notional across the selected accounts
- each participating account finishes the basket with matched long and short exposure across all traded symbols

Example:

```toml
symbols = ["BTC", "ETH"]
symbols_per_trade = 2
trade_size_usd = { min = 140, max = 160 }
position_roi_limit = 0.8   # close if any single position reaches +/-80% ROI
combined_roi_limit = 0.1   # close if the full basket reaches +/-10% ROI
```

Behavior:

- `symbols_per_trade = 1` keeps the classic single-symbol mode
- `symbols_per_trade = 2..4` enables balanced basket mode
- when `symbols_per_trade > 1`, set exactly that many entries in `symbols`
- safety exits now use both per-position ROI and combined basket ROI

### Telegram Notifications

Add a `[telegram]` section to your config to receive trade notifications:

```toml
[telegram]
token = "your-bot-token"   # from @BotFather — run `config encrypt` to encrypt
chat_id = "your-chat-id"   # personal or group chat ID

# Channels to notify (remove any to silence them):
notify = ["start", "stop", "errors", "reports"]

report_interval = "1h"  # how often to send periodic digest
```

To get credentials:

1. Create a bot via [@BotFather](https://t.me/BotFather) and copy the token
2. Get your chat ID from [@userinfobot](https://t.me/userinfobot)
3. Encrypt the token: `uv run -m apps.<app> config encrypt`
4. Test the connection: `uv run -m apps.<app> tgtest`

Notification channels:

| Channel   | When                                           |
| --------- | ---------------------------------------------- |
| `start`   | Trade cycle opens (symbol, size, accounts)     |
| `stop`    | Trade cycle closes (PnL, duration)             |
| `errors`  | Cycle failures and crashes                     |
| `reports` | Periodic digest (trades, volume, burn, $/100k) |

### Password Management

```bash
# Set password in .env to avoid prompts
echo "DF_CONFIG_PASSWORD=your-password-here" >> .env
```

## Updating

To update to the latest version:

```bash
# Stop running instances (Ctrl+C or kill the process)

# Pull latest changes
git pull

# Update dependencies
uv sync

# Restart trading
uv run -m apps.<app> trade
```

## How It Works

Delta-neutral trading maintains limited directional exposure by opening matched long/short positions.

In classic mode, one cycle opens one symbol with opposite sides across different accounts.

In basket mode, one cycle can split the trade across multiple symbols. The planner distributes notional so each symbol stays neutral and each account also nets out across the full basket.

## Recommended Services

- [**Digital Ocean**](https://m.do.co/c/a97fd963258f) – VPC for running bots 24/7 in background
- [**Proxy Shard**](https://proxyshard.com?ref=5406) – proxies for crypto activities

## Telemetry

Delta-farmer collects anonymous usage statistics (exchange name, command used,
technical config flags) to understand adoption and popular features.
No wallet addresses, balances, or strategy parameters are ever sent.
Set `DF_TELEMETRY=0` to opt out completely.

## Risk Disclaimer

**⚠️ USE AT YOUR OWN RISK**

- This software is for educational purposes
- Trading cryptocurrencies carries significant financial risk
- You may lose all deposited funds
- No guarantees of profit or airdrop eligibility
- Always test with small amounts first
- The authors are not responsible for any losses

## Contact & Feedback

I'd love to hear your feedback on usage, features, and improvements!

- **X/Twitter:** [@uid127](https://x.com/uid127)
- **Telegram:** [@eazyrekt](https://t.me/s/eazyrekt) - drop farming insights & updates
