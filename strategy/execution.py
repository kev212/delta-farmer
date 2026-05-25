# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Code so clean it squeaks
import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from lib import utils
from lib.errors import AppError
from lib.logger import logger
from lib.utils import round_to_tick_size

from .models import (
    Order,
    OrderStatus,
    Position,
    Side,
    StrategyConfig,
    TradeAction,
    TradingClient,
    opposite_side,
)


@dataclass
class PositionState:
    roi: Decimal
    pnl: Decimal
    entry_cost: Decimal
    size: Decimal


@dataclass
class CloseSafetyState:
    safe: bool
    reason: str
    spread_bps_max: Decimal
    delta_pnl_pct: Decimal
    total_notional: Decimal
    total_pnl: Decimal


# MARK: Limit order

# reference: 0.25% BBO drift → give up waiting, go to fallback
# configurable via StrategyConfig.limit_drift_pct (default 0.0025)
_DRIFT_REF = Decimal("0.0025")


# MARK: Spread & delta-PnL safety


async def _check_spread_one(
    client: TradingClient, symbol: str, max_bps: int
) -> tuple[bool, Decimal]:
    """Check spread for one symbol on one client.
    Returns (ok, spread_bps). max_bps=0 = always ok (disabled)."""
    bid, ask = await client.get_bbo(symbol)
    if bid <= 0 or ask <= 0:
        return False, Decimal(-1)
    mid = (bid + ask) / 2
    spread_bps = ((ask - bid) / mid) * Decimal(10000)
    if max_bps <= 0:
        return True, spread_bps
    return spread_bps <= max_bps, spread_bps


async def check_open_safe(
    actions_per_symbol: dict[str, list[TradeAction]],
    max_spread_bps: int,
) -> tuple[bool, str]:
    """Check spread on every leg before opening. Returns (safe, reason)."""
    if max_spread_bps <= 0:
        return True, "spread guardrail disabled"
    for symbol, acts in actions_per_symbol.items():
        for act in acts:
            ok, bps = await _check_spread_one(act.client, symbol, max_spread_bps)
            if not ok:
                return False, (
                    f"{act.client.name} {symbol} spread {bps:.1f}bps > {max_spread_bps}bps (open)"
                )
    return True, "OK"


async def check_close_safe(
    actions_per_symbol: dict[str, list[TradeAction]],
    max_spread_bps: int,
    max_delta_pnl_pct: float,
) -> CloseSafetyState:
    """Check spread and delta PnL before closing. Returns CloseSafetyState."""
    total_pnl = Decimal(0)
    total_notional = Decimal(0)
    spread_max = Decimal(0)

    for symbol, acts in actions_per_symbol.items():
        for act in acts:
            ok, bps = await _check_spread_one(act.client, symbol, max_spread_bps)
            if bps > spread_max:
                spread_max = bps
            if max_spread_bps > 0 and not ok:
                return CloseSafetyState(
                    safe=False,
                    reason=f"{act.client.name} {symbol} spread {bps:.1f}bps > {max_spread_bps}bps",
                    spread_bps_max=spread_max,
                    delta_pnl_pct=Decimal(0),
                    total_notional=total_notional,
                    total_pnl=Decimal(0),
                )

            pos = await _position_state(act.client, symbol)
            if pos is None or pos.size == 0:
                return CloseSafetyState(
                    safe=False,
                    reason=f"{act.client.name} {symbol} position missing",
                    spread_bps_max=spread_max,
                    delta_pnl_pct=Decimal(0),
                    total_notional=Decimal(0),
                    total_pnl=Decimal(0),
                )

            mid_price = await act.client.get_price(symbol)
            entry_cost = pos.size * pos.entry_price
            sign = Decimal(1) if pos.side == "bid" else Decimal(-1)
            pnl = (pos.size * mid_price - entry_cost) * sign
            total_pnl += pnl
            total_notional += entry_cost

    if total_notional == 0:
        return CloseSafetyState(
            safe=True,
            reason="no positions",
            spread_bps_max=spread_max,
            delta_pnl_pct=Decimal(0),
            total_notional=Decimal(0),
            total_pnl=Decimal(0),
        )

    delta_pct = abs(total_pnl) / total_notional
    if max_delta_pnl_pct > 0 and delta_pct > Decimal(str(max_delta_pnl_pct)):
        return CloseSafetyState(
            safe=False,
            reason=f"delta PnL {delta_pct:.3%} > {max_delta_pnl_pct:.3%}",
            spread_bps_max=spread_max,
            delta_pnl_pct=delta_pct,
            total_notional=total_notional,
            total_pnl=total_pnl,
        )

    return CloseSafetyState(
        safe=True,
        reason="OK",
        spread_bps_max=spread_max,
        delta_pnl_pct=delta_pct,
        total_notional=total_notional,
        total_pnl=total_pnl,
    )


async def wait_safe_close(
    actions_per_symbol: dict[str, list[TradeAction]],
    cfg: StrategyConfig,
    stop_event: asyncio.Event | None = None,
) -> CloseSafetyState:
    """Wait until close-safety clears or timeout. Caller must still close regardless."""
    if cfg.max_spread_close_bps <= 0 and cfg.max_delta_pnl_pct <= 0:
        return CloseSafetyState(
            safe=True,
            reason="all close guards disabled",
            spread_bps_max=Decimal(0),
            delta_pnl_pct=Decimal(0),
            total_notional=Decimal(0),
            total_pnl=Decimal(0),
        )

    started = time.time()
    state: CloseSafetyState | None = None
    iteration = 0

    while time.time() - started < cfg.close_safety_wait_sec:
        if stop_event and stop_event.is_set():
            logger.info("Stop event during close-safety wait, force close")
            break

        try:
            state = await check_close_safe(
                actions_per_symbol,
                cfg.max_spread_close_bps,
                cfg.max_delta_pnl_pct,
            )
        except Exception as e:
            logger.warning(f"Close safety check error: {type(e).__name__}: {e}")
            await asyncio.sleep(cfg.close_safety_poll_sec)
            continue

        iteration += 1
        if state.safe:
            if iteration > 1:
                logger.info(
                    f"Close gate cleared after {iteration} polls "
                    f"({time.time() - started:.0f}s) — spread={state.spread_bps_max:.1f}bps "
                    f"delta={state.delta_pnl_pct:.3%}"
                )
            return state

        elapsed = time.time() - started
        logger.warning(
            f"Close gate held [{elapsed:.0f}s/{cfg.close_safety_wait_sec}s]: "
            f"{state.reason} — waiting {cfg.close_safety_poll_sec}s"
        )
        await asyncio.sleep(cfg.close_safety_poll_sec)

    if state is None:
        state = CloseSafetyState(
            safe=False,
            reason="timeout without successful check",
            spread_bps_max=Decimal(0),
            delta_pnl_pct=Decimal(0),
            total_notional=Decimal(0),
            total_pnl=Decimal(0),
        )
    logger.error(
        f"Close gate timeout after {cfg.close_safety_wait_sec}s: {state.reason} — force closing"
    )
    return state


async def _fetch_limit_price(
    client: TradingClient, symbol: str, side: Side, tick_size: Decimal
) -> Decimal:
    """Fetch BBO and return tick-rounded price for the given side."""
    bid, ask = await client.get_bbo(symbol)
    return round_to_tick_size(bid if side == "bid" else ask, tick_size)


async def fill_limit_order(
    client: TradingClient,
    symbol: str,
    side: Side,
    qty: Decimal,
    reduce_only=False,
    timeout=60,
    use_market_fallback=True,
    drift_pct: Decimal = Decimal("0.0025"),
) -> Order | None:
    """Place limit order and wait for fill with optional market fallback."""
    tick_size = await client.get_tick_size(symbol)
    price = await _fetch_limit_price(client, symbol, side, tick_size)

    log = logger.bind(account=client.name)
    log.debug(f"Limit {side} {qty} {symbol} @ {price}")
    order = await client.limit_order(symbol, side, qty, price, reduce_only)
    if order.status == OrderStatus.FILLED:
        return order  # already filled (e.g. exchange falls back to market internally)

    order_id = order.id
    started_at, filled_since = time.time(), None
    poll_delay = 0.25  # starts at 250ms, grows to ~3s
    last_log_at = started_at
    bbo_stable_warned = False

    while True:
        await asyncio.sleep(poll_delay)
        poll_delay = min(poll_delay * 2.5, 3.0)

        order = await client.get_order(order_id)
        if order is None:
            if (time.time() - started_at) > timeout:
                raise AppError(f"Limit order {order_id} never appeared — unknown state, aborting")
            continue  # archive lag — keep polling

        elapsed = time.time() - started_at
        if time.time() - last_log_at >= 30:
            fill_pct = f" ({order.filled / order.size:.0%})" if order.filled > 0 else ""
            log.debug(f"Limit {side} {qty} {symbol}: waiting{fill_pct} elapsed={elapsed:.0f}s")
            last_log_at = time.time()

        if order.status == OrderStatus.FILLED:
            log.debug(f"Limit {side} {qty} {symbol} filled in {time.time() - started_at:.1f}s")
            return order

        if order.status == OrderStatus.CANCELED:
            elapsed = time.time() - started_at
            raise RuntimeError(
                f"Limit {symbol} canceled by exchange after {elapsed:.0f}s"
                f" (filled {order.filled}/{order.size})"
            )

        if order.filled > 0 and filled_since is None:
            filled_since = time.time()

        check_time = filled_since or started_at
        if (time.time() - check_time) > timeout:
            current_price = await _fetch_limit_price(client, symbol, side, tick_size)
            drift = abs(current_price - price) / price
            if drift <= drift_pct:
                if not bbo_stable_warned:
                    log.debug(f"Limit timeout BBO stable (drift={drift:.3%}/{drift_pct:.3%})")
                    bbo_stable_warned = True
                started_at, filled_since = time.time(), None
                continue

            bbo_stable_warned = False
            log.debug(
                f"Limit order timeout after {timeout}s (BBO drift {drift:.3%} > {drift_pct:.3%})"
            )
            await client.cancel_order(order)
            remaining = order.size - order.filled
            if use_market_fallback and remaining > 0:
                log.debug(f"Limit timeout → market fallback {side} {remaining} {symbol}")
                return await client.market_order(symbol, side, remaining, reduce_only)
            raise RuntimeError(f"Limit {symbol} timed out after {timeout}s, no fallback")


# MARK: Execution primitives


async def ensure_leverage(accounts: Sequence[TradingClient], symbol: str, leverage: int) -> None:
    async def _ensure(acc: TradingClient) -> None:
        current = await acc.get_leverage(symbol)
        if current is None or current != leverage:
            await acc.set_leverage(symbol, leverage)

    await asyncio.gather(*[_ensure(acc) for acc in accounts])


async def close_all(accs: Sequence[TradingClient], _attempts: int = 3) -> None:
    """Best-effort cleanup — retries on failure, never raises. cancel/close are idempotent."""
    n_failed = 0
    for attempt in range(_attempts):
        rs1 = await asyncio.gather(*[a.cancel_all_orders() for a in accs], return_exceptions=True)
        rs2 = await asyncio.gather(*[a.close_all_positions() for a in accs], return_exceptions=True)

        n_orders = sum(r for r in rs1 if isinstance(r, int))
        n_positions = sum(r for r in rs2 if isinstance(r, int))
        n_failed = sum(1 for r in [*rs1, *rs2] if isinstance(r, Exception))
        if n_orders + n_positions > 0:
            logger.info(f"Closed {n_orders} orders and {n_positions} positions")
        if not n_failed:
            return

        if attempt < _attempts - 1:
            logger.warning(f"close_all: {n_failed} failed, retrying ({attempt + 1}/{_attempts})...")
            await asyncio.sleep(2.0 * 2**attempt)

    logger.warning(f"close_all: still {n_failed} account(s) failed after {_attempts} attempts")


async def _fill_limit_order(
    client: TradingClient,
    symbol: str,
    side: Side,
    qty: Decimal,
    cfg: StrategyConfig,
    reduce_only: bool = False,
) -> Order | None:
    return await fill_limit_order(
        client,
        symbol,
        side,
        qty,
        reduce_only=reduce_only,
        timeout=cfg.limit_wait,
        use_market_fallback=cfg.limit_market_fallback,
        drift_pct=Decimal(str(cfg.limit_drift_pct)),
    )


async def check_min_trade_sizes(actions: list[TradeAction], symbol: str) -> None:
    """Raise if any account's trade size is below the exchange minimum."""
    min_usds = await asyncio.gather(*[act.client.get_min_trade_usd(symbol) for act in actions])
    failed = [
        (act.client.name, act.size_usd, min_usd)
        for act, min_usd in zip(actions, min_usds)
        if act.size_usd < min_usd
    ]
    if not failed:
        return
    for name, size, min_usd in failed:
        logger.warning(f"{name}: {size:.2f} < min {min_usd:.2f} USD for {symbol}")
    names = ", ".join(name for name, _, _ in failed)
    raise RuntimeError(f"Trade size below minimum for: {names}")


async def open_positions(acts: list[TradeAction], symbol: str, cfg: StrategyConfig) -> None:
    """Open positions for one symbol. With use_limit: prime account (acts[0]) fills limit first,
    then hedge accounts open via market in parallel. On limit failure with no fallback — abort."""
    all_acts = acts
    slip = Decimal(str(cfg.market_slippage_open))

    if cfg.use_limit:
        prime, acts = acts[0], acts[1:]
        prime.order = await _fill_limit_order(prime.client, symbol, prime.side, prime.qty, cfg)
        if prime.order is None:
            await close_all([act.client for act in all_acts])
            return

    rs = await asyncio.gather(
        *[act.client.market_order(symbol, act.side, act.qty, slippage=slip) for act in acts]
    )
    for act, order in zip(acts, rs):
        act.order = order
        log = logger.bind(account=act.client.name)
        log.debug(f"Market {act.side} {act.qty} {symbol} filled (slip={slip})")


async def close_symbol_positions(
    accs: list[TradingClient], symbol: str, cfg: StrategyConfig, use_limit=False
) -> None:
    """Close this cycle's positions on the given symbol only."""
    assert len(set(acc.name for acc in accs)) == len(accs), "Duplicate accounts in close_positions"

    slip = Decimal(str(cfg.market_slippage_close))

    if use_limit:
        prime, accs = accs[0], accs[1:]
        positions = await prime.positions()
        positions = [p for p in positions if p.symbol == symbol]
        for pos in positions:
            side = opposite_side(pos.side)
            await _fill_limit_order(prime, pos.symbol, side, pos.size, cfg, reduce_only=True)

    async def _close_market(acc: TradingClient) -> None:
        for pos in [p for p in await acc.positions() if p.symbol == symbol]:
            close_side = opposite_side(pos.side)
            await acc.market_order(symbol, close_side, pos.size, reduce_only=True, slippage=slip)
            logger.bind(account=acc.name).debug(f"Closed {pos.size} {symbol} market (slip={slip})")

    await asyncio.gather(*[_close_market(acc) for acc in accs])


async def _position_state(client: TradingClient, symbol: str) -> Position | None:
    positions = await client.positions()
    symbol_positions = [p for p in positions if p.symbol == symbol]

    if len(symbol_positions) != 1:
        logger.warning(f"{len(symbol_positions)} positions for {symbol} on {client.name}")
        return None

    return symbol_positions[0]


async def _position_roi(client: TradingClient, symbol: str) -> PositionState | None:
    pos = await _position_state(client, symbol)
    if pos is None or pos.size == 0:
        return None

    price = await client.get_price(symbol)
    entry_cost = pos.size * pos.entry_price
    current_cost = pos.size * price
    sign = Decimal(1) if pos.side == "bid" else Decimal(-1)
    pnl = (current_cost - entry_cost) * sign
    roi = pnl / entry_cost
    return PositionState(roi, pnl, entry_cost, pos.size)


async def positions_within_limits(
    symbol_actions: dict[str, list[TradeAction]],
    position_roi_limit: Decimal,
    combined_roi_limit: Decimal,
) -> bool:
    """Check per-position ROI and combined basket ROI. Returns False (→ emergency close_all) if:
    - any position breaches position_roi_limit (single leg drifted too far), or
    - combined PnL/entry_cost breaches combined_roi_limit (hedge breaking down at portfolio level),
    - any position disappeared (count != 1) — covers liquidation, manual close, exchange close.
    - any position size deviates >5% from expected qty — covers ADL / partial fill on open.
    """
    total_pnl = Decimal(0)
    total_entry_cost = Decimal(0)
    checks = [(symbol, act) for symbol, actions in symbol_actions.items() for act in actions]
    states = await asyncio.gather(*[_position_roi(act.client, symbol) for symbol, act in checks])

    for (symbol, act), state in zip(checks, states):
        if state is None:
            return False

        if act.qty and abs(state.size - act.qty) / act.qty > Decimal("0.05"):
            logger.info(
                f"Position {symbol} on {act.client.name}: "
                f"size {state.size} != expected {act.qty}, closing..."
            )
            return False

        if abs(state.roi) >= position_roi_limit:
            logger.info(f"Position {symbol} on {act.client.name} hit {state.roi:.2%}, closing...")
            return False

        total_pnl += state.pnl
        total_entry_cost += state.entry_cost

    if total_entry_cost == 0:
        return True

    combined_roi = total_pnl / total_entry_cost
    if abs(combined_roi) >= combined_roi_limit:
        logger.info(f"Combined ROI hit {combined_roi:.2%}, closing...")
        return False

    return True


async def hold_positions(
    symbol_actions: dict[str, list[TradeAction]],
    cfg: StrategyConfig,
    stop_event: asyncio.Event | None = None,
) -> bool:
    """Hold for trade_duration, calling positions_within_limits every heartbeat.
    Returns False on early exit (stop_event or safety breach). Tolerates transient API errors
    (logged on 2nd consecutive identical error) to avoid false-positive emergency closes.
    """
    duration = cfg.trade_duration.sample()
    logger.info(utils.wait_msg(duration))

    until = time.time() + duration
    last_error_key: str | None = None
    last_error_count = 0
    while time.time() < until:
        if stop_event and stop_event.is_set():
            logger.info("Stop event received, exiting early")
            return False

        sleep_for = min(cfg.trade_heartbeat, until - time.time())
        await asyncio.sleep(max(0, sleep_for))

        try:
            if not await positions_within_limits(
                symbol_actions,
                Decimal(str(cfg.position_roi_limit)),
                Decimal(str(cfg.combined_roi_limit)),
            ):
                return False
            last_error_key = None
            last_error_count = 0
        except Exception as e:
            error_key = f"{type(e).__name__}: {e}"
            if error_key == last_error_key:
                last_error_count += 1
            else:
                last_error_key = error_key
                last_error_count = 1

            if last_error_count == 2:
                msg = f"Position safety check failed {type(e)}: {str(e)[:200]}, continuing wait..."
                logger.warning(msg)

    return True
