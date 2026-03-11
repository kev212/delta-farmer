# Delta Farmer

Delta-neutral trading bot for multiple exchanges (Ethereal, Omni, Nado, Pacifica). It can run classic single-symbol hedges or the newer multi-symbol balanced mode that splits one cycle across 1-4 symbols while keeping both account-level and symbol-level exposure neutral.

## Project Structure

```
strategy/        # trading protocol, strategy logic, config models
  trading.py     # TradingClient protocol, Order/Position models, utilities
  delta.py       # DeltaStrategy – main trading loop; run_groups – grouped mode
  execution.py   # execution primitives: open/close positions, ROI safety checks, hold loop
  models.py      # StrategyConfig, Range, DurationSec, load_config
  planner.py     # trade planner for multi-symbol/account-balanced action sets

lib/             # generic infrastructure (no trading logic)
  utils.py       # math, time, file I/O, misc utilities
  http.py        # AsyncHttp base class with caching/proxy support
  decorators.py  # retry, ttl_cache, bind_log_context
  crypto.py      # encrypt/decrypt private keys in config
  store.py       # DataStore – persistent pickle-based state
  table.py       # AutoTable – rich terminal display
  cli.py         # create_cli(name, config_path, sec_fields) + run_app()
  logger.py      # loguru logger setup
  telemetry.py   # anonymous usage tracking (PostHog)

clients/         # one file per exchange, implements TradingClient protocol
  ethereal.py    # EVM-based, uses limit orders, signer key support
  omni.py        # EVM-based, limit orders fall back to market internally
  nado.py        # EVM-based
  pacifica.py    # Solana-based

apps/            # launchers – one file per exchange
configs/         # TOML config files (gitignored)
configs.example/ # example config files
```

## Architecture

**TradingClient** (`strategy/trading.py`) — protocol all clients implement. Strategy code depends only on the protocol, never on concrete clients. Key types: `Side = Literal["bid", "ask"]`, `Position`, `Order`.

**DeltaStrategy** (`strategy/delta.py`) — works with any list of `TradingClient` instances. Trade cycle: fetch balances → pick 1..`symbols_per_trade` symbols → build a per-symbol action plan → validate min sizes → ensure leverage → open positions symbol-by-symbol → hold with ROI safety checks → close symbol-by-symbol → cooldown → repeat. Apps use `run_groups(cfg, accs)` which handles both single and grouped mode.

**Planner** (`strategy/planner.py`) — finds a safe account combination for the sampled trade size, then distributes that size across 1-4 symbols. For multi-symbol cycles it keeps every symbol delta-neutral and also keeps each participating account net-neutral across the full basket.

**Execution primitives** (`strategy/execution.py`) — stateless async helpers used by DeltaStrategy: `ensure_leverage`, `open_positions`, `close_symbol_positions`, `close_all`, `positions_within_limits`, `hold_positions`. Safety checks now use both per-position ROI and combined basket ROI.

**StrategyConfig** (`strategy/models.py`) — base config extended by each app with an `accounts` list:

```toml
symbols               = ["BTC", "ETH"]  # symbols the strategy is allowed to trade
symbols_per_trade     = 1               # 1 = classic mode, 2-4 = balanced multi-symbol basket
leverage              = 10              # target leverage set on each participating account
trade_size_usd        = [100, 500]      # random total notional sampled for the cycle
trade_duration        = ["1m", "5m"]    # how long to hold positions before normal close
trade_cooldown        = ["30s", "2m"]   # delay between completed trade cycles
trade_heartbeat       = "15s"           # interval between safety checks while holding
position_roi_limit    = 0.8             # close if any single position reaches +/-80% ROI
combined_roi_limit    = 0.1             # close if the whole basket reaches +/-10% ROI
use_limit             = false           # use limit order for the main account instead of market
limit_wait            = "90s"           # max time to wait for limit fill handling
limit_market_fallback = true            # fall back to market when a limit attempt does not fill
first_as_main         = false           # in single-group mode, pin the first account as main
group_size            = 2               # optional: split enabled accounts into equal groups
regroup_interval      = "12h"           # optional: stop groups, rebalance, then restart
```

Use `symbols` only. Legacy `markets` is rejected and must be replaced in configs.

`symbols_per_trade > 1` requires `len(symbols) == symbols_per_trade`; the planner currently supports up to 4 symbols in one cycle.

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
- **Use `uv run`** for Python commands, scripts, tests, and tools
- **`bid/ask`** for side terminology (not buy/sell)
- **`# MARK: Section name`** for section dividers in code (not `# ---` or `# ===`)
- Client methods work with qty (base asset); USD conversion is the strategy's responsibility
- Write compact code, avoid verbose constructs
