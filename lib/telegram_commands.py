# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License

import asyncio
from decimal import Decimal
from typing import Any, Sequence

from curl_cffi.requests import AsyncSession

from strategy import TradingClient

from .logger import logger
from .telegram import _state, enabled, send
from .utils import short_addr

_HELP_TEXT = """
🤖 *Delta Farmer Commands*

📊 *Info:*
/info — Balance & points summary
/positions — Open positions per account
/spread <SYMBOL> — Current BBO spread for a symbol
/balance — Quick balance per wallet

📈 *Stats:*
/stats — Period stats summary
/uptime — Bot running time

❓ *Other:*
/help — Show this menu
"""


# MARK: Command handlers


def _auth(chat_id: Any) -> bool:
    return str(chat_id) == str(_state.cfg.chat_id)


async def _cmd_help() -> str:
    return _HELP_TEXT


async def _cmd_uptime() -> str:
    from .telegram import _uptime

    total_cycles = (
        sum(s["trades"] for s in _state.stats.values()) if hasattr(_state, "stats") else 0
    )
    return f"⏰ *Uptime:* {_uptime()}\n📊 *Cycles:* {total_cycles}"


async def _cmd_balance() -> str:
    accs = _state.accounts or []
    if not accs:
        return "❌ No accounts registered"

    lines = [f"💰 *Balance* — {len(accs)} accounts\n"]
    total = Decimal(0)
    for acc in accs:
        try:
            bal = await acc.balance()
            total += bal
            lines.append(f"  {acc.name}: `${bal:.2f}`")
        except Exception as e:
            lines.append(f"  {acc.name}: ❌ {type(e).__name__}")
    lines.append(f"\n*Total:* `${total:.2f}`")
    return "\n".join(lines)


async def _cmd_info() -> str:
    accs = _state.accounts or []
    if not accs:
        return "❌ No accounts registered"

    lines = [f"📊 *Account Summary* — {len(accs)} accounts\n"]
    total_bal = Decimal(0)
    total_vol = Decimal(0)

    for acc in accs:
        try:
            bal = await acc.balance()
            total_bal += bal
            addr = short_addr(acc.address) if hasattr(acc, "address") else "?"
            total_vol += Decimal(0)  # volume not available from balance only
            lines.append(f"  ✓ {acc.name} `{addr}` — `${bal:.2f}`")
        except Exception as e:
            lines.append(f"  ❌ {acc.name} — {type(e).__name__}")

    if total_bal:
        lines.append(f"\n*Total Balance:* `${total_bal:.2f}`")
    return "\n".join(lines)


async def _cmd_positions() -> str:
    accs = _state.accounts or []
    if not accs:
        return "❌ No accounts registered"

    lines = ["📊 *Open Positions*\n"]
    has_pos = False

    for acc in accs:
        try:
            positions = await acc.positions()
            if not positions:
                continue
            has_pos = True
            for p in positions:
                sign = "+" if p.side == "bid" else "-"
                entry_cost = p.size * p.entry_price
                price, bid, ask = Decimal(0), Decimal(0), Decimal(0)
                try:
                    bid, ask = await acc.get_bbo(p.symbol)
                    price = (bid + ask) / 2
                except Exception:
                    price = p.entry_price
                pnl = (p.size * price - entry_cost) * (1 if p.side == "bid" else -1)
                roi = pnl / entry_cost if entry_cost else Decimal(0)
                emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"  {emoji} {acc.name}: {sign}{p.size:.4f} {p.symbol} @ "
                    f"${p.entry_price:.2f} | PnL: `${pnl:.2f}` ({roi:+.2%})"
                )
        except Exception as e:
            lines.append(f"  ❌ {acc.name}: {type(e).__name__}")

    if not has_pos:
        return "📭 No open positions"

    return "\n".join(lines)


async def _cmd_spread(args: str) -> str:
    accs = _state.accounts or []
    if not accs:
        return "❌ No accounts registered"

    symbol = args.strip().upper()
    if not symbol:
        return "⚠️ Usage: /spread <SYMBOL>\nExample: `/spread BTC`"

    acc = accs[0]
    try:
        bid, ask = await acc.get_bbo(symbol)
        mid = (bid + ask) / 2
        bps = ((ask - bid) / mid) * Decimal(10000)
        return (
            f"📈 *{symbol}*\n"
            f"  Bid: `${bid:.4f}`\n"
            f"  Ask: `${ask:.4f}`\n"
            f"  Mid: `${mid:.4f}`\n"
            f"  Spread: `{bps:.2f}` bps"
        )
    except Exception as e:
        return f"❌ Error fetching {symbol}: {type(e).__name__}"


async def _cmd_stats() -> str:
    """Reuse the report format from telegram.py."""
    from .telegram import _format_report

    try:
        result = _format_report()
        return result if result.strip() else "📭 No stats yet"
    except Exception as e:
        return f"❌ Stats error: {type(e).__name__}"


# MARK: Dispatch


_COMMANDS = {
    "/help": lambda _a: _cmd_help(),
    "/start": lambda _a: _cmd_help(),
    "/info": lambda _a: _cmd_info(),
    "/balance": lambda _a: _cmd_balance(),
    "/positions": lambda _a: _cmd_positions(),
    "/stats": lambda _a: _cmd_stats(),
    "/uptime": lambda _a: _cmd_uptime(),
    "/spread": lambda a: _cmd_spread(a),
}


async def handle_message(chat_id: Any, text: str) -> str | None:
    if not _auth(chat_id):
        logger.warning(f"Unauthorized command from chat_id={chat_id}")
        return None

    text = text.strip()
    cmd = text.split()[0].lower()
    args = text[len(cmd) :].strip()

    handler = _COMMANDS.get(cmd)
    if handler is None:
        return None  # unknown command — ignore silently

    try:
        return await handler(args)
    except Exception as e:
        logger.warning(f"Command {cmd} failed: {type(e).__name__}: {e}")
        return f"❌ Command error: {type(e).__name__}"


# MARK: Polling loop


async def _polling_loop(interval: float = 1.5) -> None:
    """Long-poll Telegram for new messages and dispatch commands."""
    if not enabled() or not _state.cfg.commands_enabled:
        return

    offset = 0
    token = _state.cfg.token.get_secret_value()
    base = f"https://api.telegram.org/bot{token}"

    logger.info("Telegram command polling started")

    try:
        while True:
            try:
                url = f"{base}/getUpdates?timeout=10&offset={offset}"
                async with AsyncSession() as sess:
                    rep = await sess.get(url, timeout=15)
                data = rep.json()

                for update in data.get("result", []):
                    update_id = update.get("update_id", 0)
                    offset = update_id + 1

                    msg = update.get("message") or update.get("callback_query", {}).get("message")
                    if not msg:
                        continue

                    chat_id = msg.get("chat", {}).get("id")
                    text = msg.get("text", "")

                    if not text or not text.startswith("/"):
                        continue

                    response = await handle_message(chat_id, text)
                    if response:
                        await send(response)

            except Exception as e:
                logger.trace(f"Polling error: {type(e).__name__}: {e}")

            await asyncio.sleep(interval)

    except asyncio.CancelledError:
        logger.info("Telegram command polling stopped")
        raise


def start_polling(accounts: Sequence[TradingClient] | None = None) -> None:
    if not enabled() or not _state.cfg.commands_enabled:
        return

    _state.accounts = list(accounts) if accounts else []
    _state.stats = {}  # share stats dict reference for _cmd_stats

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_polling_loop(), name="telegram-commands")
    except RuntimeError:
        pass


# Expose _state extensions via import in telegram.py
# accounts and stats are set via this module's start_polling()
