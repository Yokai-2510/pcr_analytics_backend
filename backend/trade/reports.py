"""Daily trade reports — per-date aggregation.

`build_report(date)` computes the full report from the positions table.
For today it always recomputes; for past dates it prefers a snapshot
in `daily_trade_reports` and only recomputes when no snapshot exists.
`snapshot_today()` is called by the engine thread at shutdown so a
day's report is frozen once the worker stops.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Any

import utilities as utils

from trade import persistence

logger = logging.getLogger(__name__)


def build_report(date: str) -> dict[str, Any]:
    """Aggregate positions for ``date`` into a report dict. Recomputes
    from the positions table every time."""
    positions = persistence.positions_for_date(date)
    closed = [p for p in positions if p.get("status") == "closed"]
    open_eod = [p for p in positions if p.get("status") in ("open", "exiting")]

    wins = [p for p in closed if (p.get("pnl") or 0) > 0]
    losses = [p for p in closed if (p.get("pnl") or 0) < 0]
    breakeven = [p for p in closed if (p.get("pnl") or 0) == 0]
    gross_pnl = round(sum(p.get("pnl") or 0 for p in closed), 2)
    best = max((p.get("pnl") or 0 for p in closed), default=0)
    worst = min((p.get("pnl") or 0 for p in closed), default=0)
    win_rate = (len(wins) / len(closed)) if closed else None

    by_instrument: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
    )
    for p in closed:
        bucket = by_instrument[p["instrument"]]
        bucket["trades"] += 1
        if (p.get("pnl") or 0) > 0:
            bucket["wins"] += 1
        elif (p.get("pnl") or 0) < 0:
            bucket["losses"] += 1
        bucket["pnl"] = round(bucket["pnl"] + (p.get("pnl") or 0), 2)

    by_exit_reason = dict(Counter(p.get("exit_reason") or "unknown" for p in closed))
    by_side = dict(Counter(p.get("option_type") or "unknown" for p in closed))
    modes = sorted({p.get("mode") for p in positions if p.get("mode")})

    return {
        "date": date,
        "generated_at": utils.iso_now(),
        "modes": modes,
        "trades_total": len(positions),
        "trades_closed": len(closed),
        "trades_open_at_eod": len(open_eod),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "gross_pnl": gross_pnl,
        "best_trade_pnl": round(best, 2),
        "worst_trade_pnl": round(worst, 2),
        "by_instrument": dict(by_instrument),
        "by_exit_reason": by_exit_reason,
        "by_side": by_side,
    }


def get_or_build(date: str) -> dict[str, Any]:
    """For past dates prefer a snapshot; for today always recompute (live)."""
    today = utils.today_ist()
    if date != today:
        snap = persistence.get_daily_report(date)
        if snap:
            return snap
    return build_report(date)


def snapshot_today() -> dict[str, Any]:
    """Build and persist today's report. Called by the engine at shutdown
    so each day's numbers are frozen once the worker stops."""
    today = utils.today_ist()
    report = build_report(today)
    persistence.save_daily_report(today, report)
    logger.info(
        "Daily trade report snapshotted for %s: trades=%d pnl=%.2f",
        today, report["trades_total"], report["gross_pnl"],
    )
    return report


def snapshot_date(date: str) -> dict[str, Any]:
    """Force a recompute + persist for any date — useful from the admin API."""
    report = build_report(date)
    persistence.save_daily_report(date, report)
    return report


def list_dates() -> list[str]:
    return persistence.list_report_dates()
