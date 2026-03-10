# Delta Farmer

Delta-neutral trading bot for multiple exchanges (Ethereal, Omni, Nado, Pacifica). Opens opposite positions on different accounts to farm trading volume and points while minimizing directional risk.

## Project Structure

```
strategy/       # trading protocol, strategy logic, config models
  trading.py    # TradingClient protocol, Order/Position models, utilities
  strategy.py   # DeltaStrategy – main trading loop; run_groups – grouped mode
  models.py     # StrategyConfig, Range, DurationSec, load_config

utils/          # generic infrastructure (no trading logic)
  helpers.py    # math, time, file I/O, misc utilities
  http.py       # AsyncHttp base class with caching/proxy support
  decorators.py # retry, ttl_cache, bind_log_context
  crypto.py     # encrypt/decrypt private keys in config
  store.py      # DataStore – persistent pickle-based state
  table.py      # AutoTable – rich terminal display
  cli.py        # create_cli(name, config_path, sec_fields) + run_app()
  logger.py     # loguru logger setup
  telemetry.py  # anonymous usage tracking (PostHog)

clients/        # one file per exchange, implements TradingClient protocol
  ethereal.py   # EVM-based, uses limit orders, signer key support
  omni.py       # EVM-based, limit orders fall back to market internally
  nado.py       # EVM-based
  pacifica.py   # Solana-based

apps/           # launchers – one file per exchange
configs/        # TOML config files (gitignored)
configs.example/ # example config files
```

## Architecture

**TradingClient** (`strategy/trading.py`) — protocol all clients implement. Strategy code depends only on the protocol, never on concrete clients. Key types: `Side = Literal["bid", "ask"]`, `Position`, `Order`.

**DeltaStrategy** (`strategy/strategy.py`) — works with any list of `TradingClient` instances. Trade cycle: fetch balances → pick symbol → ensure leverage → open positions → hold (polling PnL) → close → cooldown → repeat. Apps use `run_groups(cfg, accs)` which handles both single and grouped mode.

**StrategyConfig** (`strategy/models.py`) — base config extended by each app with an `accounts` list:

```toml
markets            = ["BTC", "ETH"]
leverage           = 10
trade_size_usd     = [100, 500]     # random range per trade
trade_duration     = ["1m", "5m"]   # how long to hold
trade_cooldown     = ["30s", "2m"]  # gap between trades
trade_heartbeat    = "15s"          # PnL check interval
pnl_limit          = 0.25           # max loss fraction before force-close
use_limit          = false          # use limit orders for main account
limit_wait         = "90s"          # timeout before market fallback
first_as_main      = false          # treat first account as the "main" for limit orders
group_size         = 2              # optional: split accounts into rotating groups
regroup_interval   = "12h"          # optional: reshuffle groups interval
```

Duration fields accept `"15s"` / `"5m"` / `"1h"` strings or plain integers (seconds). Range fields accept `[min, max]` lists.

## Adding a New Exchange Client

1. Create `clients/{exchange}.py` — follow `clients/pacifica.py` as reference. Required:
   - `exchange = "{exchange}"` class variable (used by notifications)
   - `name` property
   - All `TradingClient` protocol methods (see `strategy/trading.py`)
   - Client methods work with qty in base asset — **never USD**
   - Use `bid`/`ask` side terminology; adapt to exchange's native terms internally

2. Create `apps/{exchange}.py` — follow `apps/pacifica.py` as reference. Key parts:
   - `AccountConfig` (pydantic, with `decrypt_privkey` validator) + `Config(StrategyConfig)`
   - `cli = create_cli("{exchange}", "configs/{exchange}.toml", ["privkey"])`
   - `match cli.command` handling: `trade` → `run_groups`, `close` → `close_all`, `info`/`stats` → exchange-specific
   - Entry: `run_app(main())`

## Code Quality

```bash
uv run pyright                  # type checking
uv run ruff format .            # formatting
uv run ruff check --fix .       # linting
```

Use `make lint` and `make test` periodically while working — after a logical chunk of changes, before wrapping up a task, or whenever something feels uncertain. Not required after every single edit, but don't skip them at the end of a session.

## Rules

- **No pathlib** — use `os.path` instead
- **`bid/ask`** for side terminology (not buy/sell)
- **`# MARK: Section name`** for section dividers in code (not `# ---` or `# ===`)
- Client methods work with qty (base asset); USD conversion is the strategy's responsibility
- Write compact code, avoid verbose constructs
