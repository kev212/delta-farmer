# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Powered by caffeine and stackoverflow
import asyncio
import time
from datetime import UTC, datetime
from typing import Any, Sequence

from curl_cffi.requests import AsyncSession

from .logger import logger
from .models import TgConfig
from .utils import pickle_dump, pickle_load

_STATS_PATH = ".cache/tg_stats.pkl"
_start_time = time.time()
_stats: dict[str, dict] = {}


class _State:
    exchange: str = ""
    cfg: TgConfig = TgConfig()
    accounts: Sequence[Any] = []
    stats: dict[str, dict] = {}


_state = _State()


def enabled() -> bool:
    return bool(_state.cfg.token.get_secret_value() and _state.cfg.chat_id)


def init(exchange: str, cfg: TgConfig) -> None:
    _state.exchange = exchange
    _state.cfg = cfg


# MARK: Formatting


def _uptime() -> str:
    elapsed = int(time.time() - _start_time)
    h, m = divmod(elapsed // 60, 60)
    return f"{h}h {m:02d}m"


def _fmt_pnl(pnl: float) -> str:
    return f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"


def _fmt_dur(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _format_report() -> str:
    lines = [f"📊 *Delta Farmer* — uptime {_uptime()}"]
    total_trades, total_vol = 0, 0.0

    for exchange, s in sorted(_stats.items()):
        trades, vol, pnl = s["trades"], s["volume"], s["pnl"]
        per100k = abs(pnl) / vol * 1e5 if vol else 0.0
        lines.append(
            f"\n*{exchange}*: {trades} trades · ${vol:,.0f} vol"
            f" · burn: {_fmt_pnl(pnl)} · $∕100k: ${per100k:.2f}"
        )
        total_trades += trades
        total_vol += vol

    if len(_stats) > 1:
        lines.append(f"\nTotal: {total_trades} trades · ${total_vol:,.0f} vol")

    return "\n".join(lines)


# MARK: Send


async def send(text: str, reply_to: int | None = None) -> int | None:
    if not enabled():
        return None
    try:
        token = _state.cfg.token.get_secret_value()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with AsyncSession() as s:
            pld: dict = {"chat_id": _state.cfg.chat_id, "text": text, "parse_mode": "Markdown"}
            if reply_to:
                pld["reply_to_message_id"] = reply_to
            res = await s.post(url, json=pld, timeout=10)
            return res.json().get("result", {}).get("message_id")
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return None


# MARK: Notifications


async def on_trade_start(symbols: list[str], size_usd: float, accounts: list[str]) -> int | None:
    if not enabled() or "start" not in _state.cfg.notify:
        return None
    sym_str = " / ".join(symbols)
    acc_str = ", ".join(accounts)
    msg = f"🟢 Trade started on *{_state.exchange}* — {sym_str} · ${size_usd:,.0f} · {acc_str}"
    return await send(msg)


async def on_trade_stop(
    pnl: float,
    duration: float,
    volume_usd: float,
    balances: list[tuple[str, float]],
    reply_to: int | None = None,
) -> None:
    exchange = _state.exchange
    if exchange not in _stats:
        _stats[exchange] = {"trades": 0, "volume": 0.0, "pnl": 0.0}
    _stats[exchange]["trades"] += 1
    _stats[exchange]["volume"] += volume_usd
    _stats[exchange]["pnl"] += pnl
    _save()

    if not enabled() or "stop" not in _state.cfg.notify:
        return

    bal_str = " | ".join(f"{name} ${bal:,.0f}" for name, bal in balances)
    await send(
        f"⚪ Trade done — {_fmt_pnl(pnl)} · {_fmt_dur(duration)}\n{bal_str}", reply_to=reply_to
    )


async def on_error(error: str, attempt: int, retry_in: float) -> None:
    if not enabled() or "errors" not in _state.cfg.notify:
        return

    from lib.utils import format_duration

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    msg = f"(attempt {attempt}, retry in {format_duration(retry_in)})\n`{error[:200]}`\n_{ts}_"
    msg = f"⚠️ *{_state.exchange}* — cycle failed {msg}"
    await send(msg)


async def on_crash(error: str) -> None:
    if not enabled() or "errors" not in _state.cfg.notify:
        return

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    await send(f"🔴 *{_state.exchange}* crashed\n`{error[:200]}`\n_{ts}_")


# MARK: Stats


def _save() -> None:
    pickle_dump(_STATS_PATH, dict(_stats))


# MARK: Background loop


async def _report_loop(interval: int) -> None:
    try:
        while True:
            await asyncio.sleep(interval)
            if not _stats:
                continue
            await send(_format_report())
            _stats.clear()
            _save()
    except asyncio.CancelledError:
        raise


def start(accounts: Sequence[Any] | None = None) -> None:
    if not enabled():
        return

    data = pickle_load(_STATS_PATH, delete_on_error=True)
    if isinstance(data, dict):
        _stats.update(data)

    # share stats dict with commands module
    _state.stats = _stats
    if accounts:
        _state.accounts = list(accounts)

    try:
        loop = asyncio.get_running_loop()

        if _state.cfg.commands_enabled:
            from .telegram_commands import _polling_loop

            loop.create_task(_polling_loop(), name="telegram-commands")

        if "reports" not in _state.cfg.notify:
            return

        loop.create_task(_report_loop(_state.cfg.report_interval), name="telegram-report")
    except RuntimeError:
        pass
