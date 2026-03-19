# Delta Farmer

> Update this file after any structural change without being asked.

Delta-neutral trading bot. Exchanges: Ethereal, Omni, Nado, Pacifica.

## Module map

| file | what lives there |
|------|-----------------|
| `strategy/models.py` | `Side`, `Order`, `Position`, `ProfileInfo`, `TradingClient` protocol, `TradeAction`, `StrategyConfig`, `load_config`, `usd_to_qty`, `opposite_side` |
| `strategy/execution.py` | `fill_limit_order`, `open_positions`, `close_symbol_positions`, `hold_positions`, `positions_within_limits`, `close_all` (no warmup, retries 3×) |
| `strategy/planner.py` | `plan_symbol_actions`, `calc_symbol_sizes`, `calc_total_from_pct` |
| `strategy/delta.py` | `DeltaStrategy` class only |
| `strategy/runner.py` | `run_groups` (app entry point), `close_all` (with warmup — CLI only) |
| `lib/models.py` | `DurationSec`, `SizeRange`, `TimeRange`, `TgConfig`, `AccountConfig` |
| `lib/utils.py` | math, time, file I/O, `random_partition`, `find_safe_pair`, async helpers |
| `lib/telegram.py` | notifications: `on_trade_start/stop`, `on_error`, `on_crash` |
| `clients/{exchange}.py` | implement `TradingClient` |
| `apps/{exchange}.py` | CLI launcher per exchange |

## Two `close_all` — don't mix up

- `execution.close_all` — called during trading, no warmup, retries 3× with backoff, never raises
- `runner.close_all` — called by CLI `close` command, does `warmup()` first

## Trade flow

```
run_groups (runner)
  → _check_cfg, _warmup_all, tg.start()
  → split accounts into groups (group_size or single)
  → DeltaStrategy.run() per group  [parallel asyncio tasks, staggered 10–30s]
      → trade_cycle()
          → get_balances → get_trade_size (usd or pct)
          → plan_symbol_actions → dict[symbol, list[TradeAction]]
          → check_min_trade_sizes
          → open_symbol_positions × each symbol  (ensure_leverage → open_positions)
          → hold_positions  (positions_within_limits every heartbeat)
          → close_symbol_positions × each symbol
          → report_pnl → tg.on_trade_stop
```

## Prime / hedge

First account = **prime** (limit order if `use_limit=true`). Rest = **hedge** (market always).
`first_as_prime=true` pins config order; otherwise shuffled each cycle.
`first_as_prime` is silently ignored when `group_size` is set.

## Config non-obvious bits

Full spec in `StrategyConfig` (`strategy/models.py`):
- `trade_size_usd` ⊕ `trade_size_pct` — exactly one required, mutually exclusive
- `trade_size_pct` → `calc_total_from_pct`: prime gets 50%, hedges split the other 50%, binding constraint is tightest account
- `symbols_per_trade > 1` → `len(symbols)` must equal `symbols_per_trade` (max 4)
- `group_size` must evenly divide enabled account count (max 5 accounts per group)
- `regroup_interval` → stops groups, re-sorts by balance, restarts
- Deprecated: `markets` → `symbols`, `first_as_main` → `first_as_prime`
- Durations: `"15s"` / `"5m"` / `"1h"` or int seconds; ranges: `[min, max]`

## Safety checks (positions_within_limits)

Dual-layer, runs every `trade_heartbeat`:
1. Per-leg ROI ≥ `position_roi_limit` (±80%) → emergency close
2. Combined basket ROI ≥ `combined_roi_limit` (±10%) → emergency close
3. Position count ≠ 1 for any symbol → liquidation/manual close detected → emergency close

Transient API errors: logged only on 2nd consecutive identical error, then ignored (avoid false-positive closes).

## Adding an exchange

1. `clients/{exchange}.py` — implement `TradingClient` (`strategy/models.py`). `exchange = "..."` class var. Qty in base asset only, never USD.
2. `apps/{exchange}.py` — follow `apps/pacifica.py`. `Config(StrategyConfig)` with `accounts: list[AccountConfig]`. Commands: `trade` → `run_groups`, `close` → `runner.close_all`, `info`/`stats` → custom.

## Conventions

- `os.path` not pathlib
- `uv run` for everything (pytest, pyright, ruff, scripts)
- `bid`/`ask` for sides; `prime`/`hedge` for account roles
- `# MARK: Section name` for dividers (not `# ---`)
- Compact code, no verbose constructs

## Tasks

Task files live in `tasks/`. Naming: `YYYY-MM-DD_short-name.md` (e.g. `2026-03-17_hip3-clients.md`).

Workflow:
- Sometimes a task is first **discussed and saved**, then implemented later in a fresh context.
- Sometimes implementation starts immediately after the task is written.
- At the start of a new session, read the relevant task file to restore full context before touching code.
- Task files contain: goal, research findings, API details, implementation plan with ordered steps, open questions.

## Debugging bugs

1. **Reproduce first** — before touching any code, add debug logging to see the raw data coming from the exchange. Run the app and capture a log that shows the bug.
2. **Write a failing test** — once the root cause is understood, write a test that fails (red) before applying any fix.
3. **Fix, then verify green** — apply the fix and confirm the test passes.

Never apply a fix based on guesses without a reproduction. Never skip the failing test step.

## Quality

```bash
uv run pytest && uv run pyright && uv run ruff format . && uv run ruff check --fix .
```
