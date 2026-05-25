"""Strike resolution for the trade engine.

Reads from oi_snapshots only — no broker API calls. Returns the
instrument token + reference price for the leg the engine wants to
trade. Defaults to the closest available strike when the configured
strike isn't present (slow-moving spots can land on a gap in the chain).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import data_processor
import utilities as utils

logger = logging.getLogger(__name__)

OptionType = Literal["CE", "PE"]


class StrikeInfo(dict):
    """Tiny dict subclass for readable access to the resolved leg fields."""


def _latest_chain(instrument: str, date: str) -> list[dict[str, Any]]:
    """All strikes from the most recent oi_snapshots timestamp for this
    instrument + date. Each row has both CE and PE leg columns."""
    with data_processor.connect() as conn:
        ts_row = conn.execute(
            """
            SELECT MAX(timestamp) AS ts FROM oi_snapshots
            WHERE instrument = ? AND substr(timestamp, 1, 10) = ?
            """,
            (instrument, date),
        ).fetchone()
        if not ts_row or not ts_row["ts"]:
            return []
        rows = conn.execute(
            """
            SELECT timestamp, strike, atm_strike, underlying_spot_price,
                   ce_instrument_key, ce_ltp,
                   pe_instrument_key, pe_ltp
            FROM oi_snapshots
            WHERE instrument = ? AND timestamp = ?
            ORDER BY strike
            """,
            (instrument, ts_row["ts"]),
        ).fetchall()
    return [dict(r) for r in rows]


def _pick_atm_row(chain: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The row whose strike matches the snapshot's atm_strike, or the row
    closest to underlying_spot_price as a fallback."""
    if not chain:
        return None
    atm = chain[0].get("atm_strike")
    if atm is not None:
        for row in chain:
            if row.get("strike") == atm:
                return row
    spot = chain[0].get("underlying_spot_price")
    if spot is None:
        return None
    return min(chain, key=lambda r: abs((r.get("strike") or 0) - spot))


def _step_for(instrument: str) -> float:
    cfg = utils.instrument_config(instrument)
    return float(cfg.get("strike_step") or 0)


def _nearest_row(chain: list[dict[str, Any]], target_strike: float) -> dict[str, Any] | None:
    if not chain:
        return None
    return min(chain, key=lambda r: abs((r.get("strike") or 0) - target_strike))


def resolve(
    instrument: str,
    side: OptionType,
    strike_mode: str,
    custom_strike: float | None,
    date: str,
) -> StrikeInfo | None:
    """Return token + reference LTP for the leg to trade, or None if we
    don't have data for this instrument yet today."""
    chain = _latest_chain(instrument, date)
    if not chain:
        return None

    atm_row = _pick_atm_row(chain)
    if atm_row is None:
        return None
    atm_strike = float(atm_row["strike"])
    step = _step_for(instrument)

    if strike_mode == "atm" or step <= 0:
        target = atm_strike
    elif strike_mode == "atm_plus_1":
        # OTM: higher strike for CE (further from spot), lower for PE
        target = atm_strike + step if side == "CE" else atm_strike - step
    elif strike_mode == "atm_minus_1":
        # ITM: lower strike for CE, higher for PE
        target = atm_strike - step if side == "CE" else atm_strike + step
    elif strike_mode == "custom" and custom_strike is not None:
        target = float(custom_strike)
    else:
        target = atm_strike

    row = _nearest_row(chain, target)
    if row is None:
        return None

    if side == "CE":
        token = row.get("ce_instrument_key")
        ltp = row.get("ce_ltp")
    else:
        token = row.get("pe_instrument_key")
        ltp = row.get("pe_ltp")

    if not token or ltp is None or ltp <= 0:
        return None

    return StrikeInfo(
        instrument=instrument,
        strike=float(row["strike"]),
        atm_strike=atm_strike,
        target_strike=target,
        side=side,
        token=str(token),
        ltp=float(ltp),
        underlying_spot=row.get("underlying_spot_price"),
        chain_timestamp=row.get("timestamp"),
    )


def latest_ltp(
    instrument: str,
    strike: float,
    side: OptionType,
    date: str,
) -> float | None:
    """LTP for a specific (instrument, strike, side) from the most recent
    oi_snapshots row that contains it. Used by the exit engine to evaluate
    open positions."""
    column = "ce_ltp" if side == "CE" else "pe_ltp"
    with data_processor.connect() as conn:
        row = conn.execute(
            f"""
            SELECT {column} AS ltp, timestamp
            FROM oi_snapshots
            WHERE instrument = ?
              AND substr(timestamp, 1, 10) = ?
              AND ABS(strike - ?) < 0.01
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (instrument, date, float(strike)),
        ).fetchone()
    if not row:
        return None
    ltp = row["ltp"]
    if ltp is None or ltp <= 0:
        return None
    return float(ltp)
