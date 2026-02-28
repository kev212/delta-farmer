# Delta Farmer

Delta-neutral trading bot for multiple exchanges (Ethereal, Omni, Pacifica). Opens opposite positions on different accounts to farm trading volume and points while minimizing directional risk.

## Project Structure

```
strategy/       # trading protocol, strategy logic, config models
  trading.py    # TradingClient protocol, Order/Position models, utilities
  strategy.py   # DeltaStrategy – main trading loop
  models.py     # StrategyConfig, Range, DurationSec, load_config

utils/          # generic infrastructure (no trading logic)
  helpers.py    # math, time, file I/O, misc utilities
  http.py       # AsyncHttp base class with caching/proxy support
  decorators.py # retry, ttl_cache, bind_log_context
  crypto.py     # encrypt/decrypt private keys in config
  store.py      # DataStore – persistent pickle-based state
  table.py      # AutoTable – rich terminal display
  cli.py        # CLI argument parser for apps
  logger.py     # loguru logger setup

clients/        # one file per exchange, implements TradingClient protocol
  ethereal.py   # EVM-based, uses limit orders, signer key support
  omni.py       # EVM-based, limit orders fall back to market internally
  pacifica.py   # Solana-based

apps/           # launchers – one directory per exchange (or strategy)
config/         # YAML/TOML config files (gitignored)
docs/           # additional documentation
```

## Architecture

### TradingClient Protocol (`strategy/trading.py`)

All exchange clients implement this protocol. Strategy code depends only on the protocol, never on concrete clients.

```python
Side = Literal["bid", "ask"]

class Position(BaseModel):
    id: str
    symbol: str
    side: Side
    size: Decimal        # always positive, base asset
    entry_price: Decimal
    unrealized_pnl: Decimal = Decimal(0)

class Order(BaseModel):
    id: str
    symbol: str
    side: Side
    size: Decimal        # total size
    filled: Decimal      # filled size
    price: Decimal | None  # None = market order
    status: str          # open, filled, cancelled, etc.
    reduce_only: bool = False

@runtime_checkable
class TradingClient(Protocol):
    @property
    def name(self) -> str: ...

    async def warmup(self) -> None: ...          # init session, auth, fetch market info
    async def balance(self) -> Decimal: ...       # available USD balance
    async def get_price(self, symbol) -> Decimal: ...
    async def get_lot_size(self, symbol) -> Decimal: ...   # min qty increment
    async def get_tick_size(self, symbol) -> Decimal: ...  # min price increment

    async def positions(self) -> list[Position]: ...
    async def close_position(self, position) -> bool: ...

    async def market_order(self, symbol, side, qty, reduce_only=False) -> Order: ...
    async def limit_order(self, symbol, side, qty, price, reduce_only=False) -> Order: ...
    async def cancel_order(self, order) -> bool: ...
    async def get_order(self, order_id) -> Order | None: ...

    async def cancel_all_orders(self) -> int: ...
    async def close_all_positions(self) -> int: ...

    async def get_leverage(self, symbol: str) -> int | None: ...  # None if exchange can't report it
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...
```

**Key design rules:**

- Client methods always work with qty in base asset. USD-to-qty conversion (`usd_to_qty`) is done by the strategy.
- If an exchange doesn't benefit from limit orders (e.g., Omni), `limit_order` internally falls back to `market_order`.
- Use `bid/ask` as canonical side terminology. Adapt in the client if the exchange uses different terms:
  ```python
  def _to_native(self, side: Side) -> str:
      return "buy" if side == "bid" else "sell"
  ```

### DeltaStrategy (`strategy/strategy.py`)

Works with any list of `TradingClient` instances — can mix exchanges.

```python
class DeltaStrategy:
    def __init__(self, cfg: StrategyConfig, accounts: list[TradingClient]): ...
    async def run(self): ...           # main loop: warmup → trade cycles → handle errors
    async def trade_cycle(self): ...   # get balances → open → wait+check PnL → close → report
```

Trade cycle:

1. Fetch balances, pick symbol (`find_safe_pair`)
2. Calculate quantities from USD via `usd_to_qty`
3. Ensure leverage (`get_leverage` → `set_leverage` if needed)
4. Open positions (limit for main account, market for rest, or all market)
5. Hold for `trade_duration`, polling every `trade_heartbeat`; bail if PnL exceeds `pnl_limit`
6. Close positions
7. Wait `trade_cooldown`, repeat

### StrategyConfig (`strategy/models.py`)

Base config shared by all apps. Apps extend it with account lists.

```toml
markets            = ["BTC", "ETH"]
leverage           = 10
trade_size_usd     = [100, 500]      # random range per trade
trade_duration     = ["1m", "5m"]   # how long to hold
trade_cooldown     = ["30s", "2m"]  # gap between trades
trade_heartbeat    = "15s"          # PnL check interval
pnl_limit          = 0.25           # max loss fraction before force-close

use_limit          = true           # use limit orders for main account
limit_wait         = "60s"          # timeout before market fallback
limit_market_fallback = true
first_as_main      = false          # treat first account as the "main" for limit orders
```

Duration fields accept strings like `"15s"`, `"5m"`, `"1h"` or plain integers (seconds).
Range fields accept `[min, max]` lists.

## Adding a New Exchange Client

1. Create `clients/{exchange}.py` implementing `TradingClient`:

```python
# delta-farmer | https://github.com/vladkens/delta-farmer
from decimal import Decimal
from utils import helpers as utils
from utils.logger import logger
from utils.decorators import bind_log_context, retry
from utils.http import AsyncHttp, HttpMethod
from strategy.trading import Order, Position, Side, opposite_side

class Client:
    def __init__(self, name: str, privkey: str, ...):
        self._name = name
        ...

    @property
    def name(self) -> str:
        return self._name

    # mark: lifecycle

    async def warmup(self) -> None: ...

    # mark: account

    async def balance(self) -> Decimal: ...

    # mark: market info

    async def get_price(self, symbol: str) -> Decimal: ...
    async def get_lot_size(self, symbol: str) -> Decimal: ...
    async def get_tick_size(self, symbol: str) -> Decimal: ...

    # mark: positions

    async def positions(self) -> list[Position]: ...

    async def close_position(self, position: Position) -> bool:
        await self.market_order(position.symbol, opposite_side(position.side), position.size, reduce_only=True)
        return True

    async def close_all_positions(self) -> int:
        positions = await self.positions()
        for p in positions:
            await self.close_position(p)
        return len(positions)

    # mark: orders

    async def market_order(self, symbol, side, qty, reduce_only=False) -> Order: ...
    async def limit_order(self, symbol, side, qty, price, reduce_only=False) -> Order: ...
    async def cancel_order(self, order: Order) -> bool: ...
    async def get_order(self, order_id: str) -> Order | None: ...
    async def cancel_all_orders(self) -> int: ...

    # mark: leverage

    async def get_leverage(self, symbol: str) -> int | None: ...  # return None if not supported
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...
```

2. Create `apps/{exchange}/config.py`:

```python
from pydantic import BaseModel, Field, SecretStr, field_validator
from utils.crypto import decrypt_value, is_encrypted
from strategy.models import StrategyConfig, load_config

class AccountConfig(BaseModel):
    name: str
    privkey: SecretStr = Field(repr=False)
    proxy: str | None = None
    enabled: bool = True

    @field_validator("privkey", mode="before")
    @classmethod
    def decrypt_privkey(cls, v: str) -> str:
        return decrypt_value(v) if isinstance(v, str) and is_encrypted(v) else v

class Config(StrategyConfig):
    accounts: list[AccountConfig]

    @classmethod
    def load(cls, filepath: str):
        return load_config(cls, filepath)
```

3. Create `apps/{exchange}/__main__.py`:

```python
# delta-farmer | https://github.com/vladkens/delta-farmer
import asyncio
from utils.cli import create_cli
from strategy.strategy import DeltaStrategy
from clients.{exchange} import Client
from .config import AccountConfig, Config

def client_from_config(cfg: AccountConfig) -> Client:
    return Client(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

async def main():
    cli = create_cli("{exchange}", "configs/{exchange}.toml")
    cfg = Config.load(cli.config)

    match cli.command:
        case "trade":
            accounts = [client_from_config(x) for x in cfg.accounts if x.enabled]
            strategy = DeltaStrategy(cfg, accounts)
            await strategy.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
```

## Adding a New Strategy

Strategies depend only on `TradingClient` — they are exchange-agnostic. Create `strategy/{name}.py`:

```python
from strategy.trading import TradingClient, Side, opposite_side, usd_to_qty
from strategy.models import StrategyConfig

class MyStrategy:
    def __init__(self, cfg: StrategyConfig, accounts: list[TradingClient]):
        self.cfg = cfg
        self.accounts = accounts

    async def run(self): ...
```

Launch from any app by replacing `DeltaStrategy` with your strategy.

## Cross-Exchange Apps

Since strategies accept any `TradingClient`, a single app can mix clients from multiple exchanges:

```python
from clients.omni import Client as OmniClient
from clients.pacifica import Client as PacificaClient

accounts = [
    OmniClient.from_config(omni_cfg),
    PacificaClient.from_config(pac_cfg),
]
strategy = DeltaStrategy(cfg, accounts)
```

Create `apps/{combo}/` for dedicated cross-exchange launchers.

## Code Quality

```bash
uv run pyright                  # type checking
uv run ruff format .            # formatting
uv run ruff check --fix .       # linting
```

## Rules

- **No pathlib** — use `os.path` instead
- **`bid/ask`** for side terminology (not buy/sell)
- **`# MARK: Section name`** for section dividers in code (not `# ---` or `# ===`)
- Client methods work with qty (base asset); USD conversion is the strategy's responsibility
- Write compact code, avoid verbose constructs
