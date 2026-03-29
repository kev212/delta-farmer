#!/usr/bin/env python3
"""Weekly trading report — reads from local cache (.cache/).

To refresh data for an exchange, run its stats command first:
  uv run apps/hyena.py stats
  uv run apps/nado.py stats
  ... (same for other exchanges)

Usage:
  uv run scripts/weekly.py              # snapshot: all exchanges, latest week
  uv run scripts/weekly.py -1           # snapshot: one week back
  uv run scripts/weekly.py -e Hyena     # Hyena: all periods (vol/burn/pts)
  uv run scripts/weekly.py --burn       # burn pivot: all exchanges × ISO weeks
"""

import argparse
import os
import pickle
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from lib.table import AutoTable, Column

CACHE = os.path.join(os.path.dirname(__file__), "..", ".cache")
ONYX_SINCE = datetime(2026, 3, 1, tzinfo=UTC)


# MARK: Shared utils


def load_pkl(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, "rb") as fh:
        data = pickle.load(fh)
    return list(data.get("records", {}).values())


def glob_cache(prefix: str, suffix: str) -> list[str]:
    try:
        files = os.listdir(CACHE)
    except FileNotFoundError:
        return []
    return [os.path.join(CACHE, f) for f in files if f.startswith(prefix) and f.endswith(suffix)]


def parse_dt(s) -> datetime:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(s).rstrip("Z")).replace(tzinfo=UTC)


# MARK: Period helpers


def _week_date_range(week_start: datetime) -> str:
    return f"{week_start.strftime('%b%d')}-{(week_start + timedelta(days=6)).strftime('%b%d')}"


def _period_week(dt: datetime, genesis: datetime) -> str:
    n = (dt - genesis).days // 7 + 1
    if n <= 0:
        return f"OFF {_week_date_range(genesis + timedelta(weeks=n - 1))}"
    return f"W{n:02d} {_week_date_range(genesis + timedelta(weeks=n - 1))}"


_NADO_EPOCHS = [
    ("ALP", datetime(2025, 11, 20, tzinfo=UTC)),
    ("OFF", datetime(2026, 1, 16, tzinfo=UTC)),
]
_NADO_W1 = datetime(2026, 1, 30, tzinfo=UTC)


def _period_nado(dt: datetime) -> str:
    for i, (prefix, since) in enumerate(_NADO_EPOCHS):
        until = _NADO_EPOCHS[i + 1][1] if i + 1 < len(_NADO_EPOCHS) else _NADO_W1
        if since <= dt < until:
            return f"{prefix} {_week_date_range(since)}"
    if dt >= _NADO_W1:
        n = (dt - _NADO_W1).days // 7 + 1
        return f"W{n:02d} {_week_date_range(_NADO_W1 + timedelta(weeks=n - 1))}"
    return dt.strftime("%Y-%m-%d")


def _to_iso_week(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


# MARK: Stats extractors (vol + burn, native periods)

Stats = dict[str, tuple[Decimal, Decimal]]  # {period_label: (vol, burn)}
Pts = dict[str, Decimal]  # {period_label: points}


def ethereal_stats() -> Stats:
    G = datetime(2025, 12, 18, tzinfo=UTC)
    out: Stats = {}
    for path in glob_cache("ethereal_", "_trades.pkl"):
        for r in load_pkl(path):
            lbl = _period_week(parse_dt(r["created_at"]), G)
            vol, burn = out.get(lbl, (Decimal(0), Decimal(0)))
            vol += Decimal(str(r["total_inc"])) + Decimal(str(r["total_dec"]))
            pnl = (
                Decimal(str(r["realized_pnl"]))
                - Decimal(str(r["fees_usd"]))
                - Decimal(str(r["funding_usd"]))
            )
            out[lbl] = (vol, burn - pnl)
    return out


def nado_stats() -> Stats:
    out: Stats = {}
    for path in glob_cache("nado_", "_trades.pkl"):
        for r in load_pkl(path):
            lbl = _period_nado(parse_dt(r["created_at"]))
            vol, burn = out.get(lbl, (Decimal(0), Decimal(0)))
            vol += Decimal(str(r["amount"])) * Decimal(str(r["price"]))
            pnl = Decimal(str(r["realized_pnl"])) - Decimal(str(r["fee"]))
            out[lbl] = (vol, burn - pnl)
    return out


def omni_stats() -> Stats:
    G = datetime(2025, 12, 11, tzinfo=UTC)
    vols: defaultdict[str, Decimal] = defaultdict(Decimal)
    burns: defaultdict[str, Decimal] = defaultdict(Decimal)
    for path in glob_cache("omni_", "_trades.pkl"):
        for r in load_pkl(path):
            if r.get("status") != "confirmed":
                continue
            lbl = _period_week(parse_dt(r["created_at"]), G)
            vols[lbl] += Decimal(str(r["price"])) * Decimal(str(r["qty"]))
    for path in glob_cache("omni_", "_transfers.pkl"):
        for r in load_pkl(path):
            if r.get("status") != "confirmed" or r.get("transfer_type") not in (
                "funding",
                "realized_pnl",
            ):
                continue
            lbl = _period_week(parse_dt(r["created_at"]), G)
            burns[lbl] -= Decimal(str(r["qty"]))
    all_lbls = set(vols) | set(burns)
    return {lbl: (vols[lbl], burns[lbl]) for lbl in all_lbls}


def pacifica_stats() -> Stats:
    G = datetime(2025, 9, 4, tzinfo=UTC)
    out: Stats = {}
    for path in glob_cache("pacifica_", "_trades.pkl"):
        for r in load_pkl(path):
            lbl = _period_week(parse_dt(r["created_at"]), G)
            vol, burn = out.get(lbl, (Decimal(0), Decimal(0)))
            vol += Decimal(str(r["amount"])) * Decimal(str(r["price"]))
            out[lbl] = (vol, burn - Decimal(str(r["pnl"])))
    return out


def zero1_stats() -> Stats:
    G = datetime(2026, 2, 3, tzinfo=UTC)
    vols: defaultdict[str, Decimal] = defaultdict(Decimal)
    burns: defaultdict[str, Decimal] = defaultdict(Decimal)
    seen: set[str] = set()
    for path in glob_cache("zero1_", "_trades_maker.pkl") + glob_cache(
        "zero1_", "_trades_taker.pkl"
    ):
        for r in load_pkl(path):
            tid = str(r.get("tradeId", r.get("uid", "")))
            if tid in seen:
                continue
            seen.add(tid)
            lbl = _period_week(parse_dt(r["time"]), G)
            vols[lbl] += Decimal(str(r["price"])) * Decimal(str(r["baseSize"]))
            if "fee" in r:
                burns[lbl] += Decimal(str(r["fee"]))
    for path in glob_cache("zero1_", "_history_pnl.pkl"):
        for r in load_pkl(path):
            lbl = _period_week(parse_dt(r["time"]), G)
            burns[lbl] -= Decimal(str(r["tradingPnl"]))
    for path in glob_cache("zero1_", "_history_funding.pkl"):
        for r in load_pkl(path):
            lbl = _period_week(parse_dt(r["time"]), G)
            burns[lbl] -= Decimal(str(r["fundingPnl"]))
    all_lbls = set(vols) | set(burns)
    return {lbl: (vols[lbl], burns[lbl]) for lbl in all_lbls}


def hyena_stats() -> Stats:
    G = datetime(2025, 12, 4, tzinfo=UTC)
    out: Stats = {}
    for path in glob_cache("hyena_", "_fills.pkl"):
        for r in load_pkl(path):
            dt = datetime.fromtimestamp(r["time"] / 1000, tz=UTC)
            lbl = _period_week(dt, G)
            vol, burn = out.get(lbl, (Decimal(0), Decimal(0)))
            vol += Decimal(str(r["px"])) * Decimal(str(r["sz"]))
            out[lbl] = (vol, burn - Decimal(str(r.get("closedPnl", 0))))
    return out


def onyx_stats() -> Stats:
    G = datetime(2026, 3, 1, tzinfo=UTC)
    out: Stats = {}
    for path in glob_cache("onyx_", "_fills.pkl"):
        for r in load_pkl(path):
            dt = datetime.fromtimestamp(r["time"] / 1000, tz=UTC)
            if dt < ONYX_SINCE:
                continue
            lbl = _period_week(dt, G)
            vol, burn = out.get(lbl, (Decimal(0), Decimal(0)))
            vol += Decimal(str(r["px"])) * Decimal(str(r["sz"]))
            out[lbl] = (vol, burn - Decimal(str(r.get("closedPnl", 0))))
    return out


# MARK: Points extractors


def _pts_by_period(prefix: str, suffix: str, dt_key: str, pts_keys: list[str], period_fn) -> Pts:
    out: defaultdict[str, Decimal] = defaultdict(Decimal)
    for path in glob_cache(prefix, suffix):
        for r in load_pkl(path):
            lbl = period_fn(parse_dt(r[dt_key]))
            out[lbl] += sum(Decimal(str(r[k])) for k in pts_keys if k in r)
    return dict(out)


def ethereal_pts() -> Pts:
    G = datetime(2025, 12, 18, tzinfo=UTC)
    return _pts_by_period(
        "ethereal_",
        "_points.pkl",
        "started_at",
        ["points", "referral_points"],
        lambda dt: _period_week(dt, G),
    )


def nado_pts() -> Pts:
    return _pts_by_period("nado_", "_points.pkl", "since", ["points"], _period_nado)


def omni_pts() -> Pts:
    G = datetime(2025, 12, 11, tzinfo=UTC)
    return _pts_by_period(
        "omni_",
        "_points.pkl",
        "start_window",
        ["total_points"],
        lambda dt: _period_week(dt, G),
    )


def pacifica_pts() -> Pts:
    G = datetime(2025, 9, 4, tzinfo=UTC)
    return _pts_by_period(
        "pacifica_",
        "_points.pkl",
        "start_window",
        ["total_points"],
        lambda dt: _period_week(dt, G),
    )


def zero1_pts() -> Pts:
    G = datetime(2026, 2, 3, tzinfo=UTC)
    return _pts_by_period(
        "zero1_",
        "_points.pkl",
        "start_window",
        ["points"],
        lambda dt: _period_week(dt, G),
    )


def hyena_pts() -> Pts:
    out: defaultdict[str, Decimal] = defaultdict(Decimal)
    for path in glob_cache("hyena_", "_rewards.pkl"):
        for r in load_pkl(path):
            out[r["period"]] += Decimal(str(r["enaxPoints"]))
    return dict(out)


# MARK: Burn extractors (ISO weeks, for burn pivot view)

BurnWeeks = defaultdict  # {iso_week: burn}


def ethereal_burn_weeks() -> BurnWeeks:
    weeks: defaultdict[str, Decimal] = defaultdict(Decimal)
    for path in glob_cache("ethereal_", "_trades.pkl"):
        for r in load_pkl(path):
            dt = parse_dt(r["created_at"])
            pnl = (
                Decimal(str(r["realized_pnl"]))
                - Decimal(str(r["fees_usd"]))
                - Decimal(str(r["funding_usd"]))
            )
            weeks[_to_iso_week(dt)] -= pnl
    return weeks


def hyena_burn_weeks() -> BurnWeeks:
    weeks: defaultdict[str, Decimal] = defaultdict(Decimal)
    for path in glob_cache("hyena_", "_fills.pkl"):
        for r in load_pkl(path):
            if not r.get("coin", "").startswith("hyna:"):
                continue
            dt = datetime.fromtimestamp(r["time"] / 1000, tz=UTC)
            weeks[_to_iso_week(dt)] -= Decimal(str(r.get("closedPnl", 0)))
    return weeks


def nado_burn_weeks() -> BurnWeeks:
    weeks: defaultdict[str, Decimal] = defaultdict(Decimal)
    for path in glob_cache("nado_", "_trades.pkl"):
        for r in load_pkl(path):
            dt = parse_dt(r["created_at"])
            pnl = Decimal(str(r["realized_pnl"])) - Decimal(str(r["fee"]))
            weeks[_to_iso_week(dt)] -= pnl
    return weeks


def omni_burn_weeks() -> BurnWeeks:
    weeks: defaultdict[str, Decimal] = defaultdict(Decimal)
    for path in glob_cache("omni_", "_transfers.pkl"):
        for r in load_pkl(path):
            if r.get("status") != "confirmed":
                continue
            if r.get("transfer_type") not in ("funding", "realized_pnl"):
                continue
            dt = parse_dt(r["created_at"])
            weeks[_to_iso_week(dt)] -= Decimal(str(r["qty"]))
    return weeks


def onyx_burn_weeks() -> BurnWeeks:
    weeks: defaultdict[str, Decimal] = defaultdict(Decimal)
    for path in glob_cache("onyx_", "_fills.pkl"):
        for r in load_pkl(path):
            if ":" in r.get("coin", ""):
                continue
            dt = datetime.fromtimestamp(r["time"] / 1000, tz=UTC)
            if dt < ONYX_SINCE:
                continue
            weeks[_to_iso_week(dt)] -= Decimal(str(r.get("closedPnl", 0)))
    return weeks


def pacifica_burn_weeks() -> BurnWeeks:
    weeks: defaultdict[str, Decimal] = defaultdict(Decimal)
    for path in glob_cache("pacifica_", "_trades.pkl"):
        for r in load_pkl(path):
            dt = parse_dt(r["created_at"])
            weeks[_to_iso_week(dt)] -= Decimal(str(r["pnl"]))
    return weeks


def zero1_burn_weeks() -> BurnWeeks:
    weeks: defaultdict[str, Decimal] = defaultdict(Decimal)
    for path in glob_cache("zero1_", "_history_pnl.pkl"):
        for r in load_pkl(path):
            dt = parse_dt(r["time"])
            weeks[_to_iso_week(dt)] -= Decimal(str(r["tradingPnl"]))
    for path in glob_cache("zero1_", "_history_funding.pkl"):
        for r in load_pkl(path):
            dt = parse_dt(r["time"])
            weeks[_to_iso_week(dt)] -= Decimal(str(r["fundingPnl"]))
    return weeks


# MARK: Exchange registry

# genesis=None → use pts > 0 as "completed" signal
# genesis=datetime → no pts, filter by epoch_end <= now
EXCHANGES: list[tuple[str, Any, Any, datetime | None]] = [
    ("Ethereal", ethereal_stats, ethereal_pts, None),
    ("Hyena", hyena_stats, hyena_pts, datetime(2025, 12, 4, tzinfo=UTC)),
    ("Nado", nado_stats, nado_pts, None),
    ("Omni", omni_stats, omni_pts, None),
    ("Onyx", onyx_stats, lambda: {}, datetime(2026, 3, 1, tzinfo=UTC)),
    ("Pacifica", pacifica_stats, pacifica_pts, None),
    ("Zero1", zero1_stats, zero1_pts, None),
]

BURN_EXCHANGES = [
    ("Ethereal", ethereal_burn_weeks),
    ("Hyena", hyena_burn_weeks),
    ("Nado", nado_burn_weeks),
    ("Omni", omni_burn_weeks),
    ("Onyx", onyx_burn_weeks),
    ("Pacifica", pacifica_burn_weeks),
    ("Zero1", zero1_burn_weeks),
]


# MARK: Views


def _epoch_end(lbl: str, genesis: datetime) -> datetime:
    if lbl.startswith("W") and lbl[1:3].isdigit():
        return genesis + timedelta(weeks=int(lbl[1:3]))
    return datetime.min.replace(tzinfo=UTC)


def _select_label(labels: list[str], week_arg: int) -> str | None:
    if not labels:
        return None
    sorted_lbls = sorted(labels)
    idx = len(sorted_lbls) - 1 + week_arg  # 0→last, -1→second-to-last
    return sorted_lbls[idx] if idx >= 0 else None


def _available_labels(
    periods: Stats, pts_map: Pts, genesis: datetime | None, now: datetime
) -> list[str]:
    if pts_map:
        with_pts = {k: v for k, v in pts_map.items() if v > 0}
        return sorted(with_pts) if with_pts else sorted(pts_map)
    if periods and genesis:
        completed = [k for k in periods if _epoch_end(k, genesis) <= now]
        return sorted(completed) if completed else sorted(periods)
    return sorted(periods)


def snapshot_view(week_arg: int) -> int:
    """All exchanges, one selected week."""
    now = datetime.now(UTC)
    tbl = AutoTable(
        Column("Exchange", justify="left"),
        Column("Period", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.1f}", total=sum),
        Column(
            "$/pt",
            "{:,.4f}",
            compute=lambda r: r["Burn"] / r["Points"],
            guard=lambda r: r["Points"] > 0,
        ),
    )
    any_data = False
    for name, stats_fn, pts_fn, genesis in EXCHANGES:
        periods = stats_fn()
        pts_map = pts_fn()
        if not periods and not pts_map:
            continue
        available = _available_labels(periods, pts_map, genesis, now)
        lbl = _select_label(available, week_arg)
        if lbl is None:
            continue
        vol, burn = periods.get(lbl, (Decimal(0), Decimal(0)))
        pts = pts_map.get(lbl, Decimal(0))
        if not vol and not burn and not pts:
            continue
        tbl.add_row(name, lbl, vol, burn, pts)
        any_data = True
    if not any_data:
        print("No cached data found.", file=sys.stderr)
        return 1
    tbl.print()
    return 0


def exchange_view(name: str) -> int:
    """One exchange, all available periods."""
    now = datetime.now(UTC)
    match = next((e for e in EXCHANGES if e[0].lower() == name.lower()), None)
    if match is None:
        names = ", ".join(e[0] for e in EXCHANGES)
        print(f"Unknown exchange {name!r}. Available: {names}", file=sys.stderr)
        return 1
    exch_name, stats_fn, pts_fn, genesis = match
    periods = stats_fn()
    pts_map = pts_fn()
    if not periods and not pts_map:
        print("No cached data found.", file=sys.stderr)
        return 1
    available = _available_labels(periods, pts_map, genesis, now)
    tbl = AutoTable(
        Column("Period", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.1f}", total=sum),
        Column(
            "$/pt",
            "{:,.4f}",
            compute=lambda r: r["Burn"] / r["Points"],
            guard=lambda r: r["Points"] > 0,
        ),
    )
    any_data = False
    for lbl in available:
        vol, burn = periods.get(lbl, (Decimal(0), Decimal(0)))
        pts = pts_map.get(lbl, Decimal(0))
        if not vol and not burn and not pts:
            continue
        tbl.add_row(lbl, vol, burn, pts)
        any_data = True
    if not any_data:
        print("No cached data found.", file=sys.stderr)
        return 1
    print(f"{exch_name} — all periods")
    tbl.print()
    return 0


def burn_view() -> int:
    """Burn pivot: rows=ISO weeks, cols=exchanges."""
    data = {name: fn() for name, fn in BURN_EXCHANGES}
    all_weeks = sorted({w for d in data.values() for w in d})
    if not all_weeks:
        print("No cached data found.", file=sys.stderr)
        return 1
    active = [name for name, _ in BURN_EXCHANGES if any(v != 0 for v in data[name].values())]
    tbl = AutoTable(
        Column("Week", justify="left"),
        *[Column(name, "{:,.2f}", total=sum) for name in active],
        Column("Total", "{:,.2f}", compute=lambda r: sum(r[n] for n in active)),
    )
    for week in all_weeks:
        tbl.add_row(week, *[data[name].get(week, Decimal(0)) for name in active])
    tbl.print()
    return 0


# MARK: Main


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly trading report")
    parser.add_argument(
        "week",
        nargs="?",
        type=int,
        default=0,
        help="0/omit=latest finished; -N=N weeks back (e.g. -1, -2)",
    )
    parser.add_argument(
        "-e", "--exchange", metavar="NAME", help="show all periods for one exchange"
    )
    parser.add_argument("--burn", action="store_true", help="burn pivot: all exchanges × ISO weeks")
    args = parser.parse_args()

    if args.burn:
        rc = burn_view()
    elif args.exchange:
        rc = exchange_view(args.exchange)
    else:
        rc = snapshot_view(args.week)

    if rc == 0:
        print("\033[2m  · cached data — run stats <exchange> to refresh\033[0m")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
