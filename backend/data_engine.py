"""Compute engine: aggregates raw snapshots into pre-computed minute ticks.

This module implements the second layer of the two-layer data architecture:
  1. Raw fetch every 30s -> stored in oi_snapshots (per-strike archive)
  2. Computed ticks every 60s -> pre-aggregated metrics in computed_ticks table

Each computed tick represents one row per minute per instrument with all
derived metrics (PCR, OI changes, cumulative changes, signals, etc.)
"""

from __future__ import annotations

import logging
from typing import Any

import data_processor
import utilities as utils

logger = logging.getLogger(__name__)


def compute_tick(
    instrument: str,
    date: str,
    raw_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]] | None = None,
    prev_tick: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Compute a single aggregated tick from the latest raw snapshot data.

    Args:
        instrument: Normalized instrument name (e.g. 'nifty')
        date: ISO date string (e.g. '2026-05-19')
        raw_rows: Latest raw snapshot rows for this instrument at one timestamp
        baseline_rows: Post-settlement baseline rows for change calculation
        prev_tick: Previous computed tick (for signal/crossover detection)

    Returns:
        Dict matching computed_ticks columns, or None if insufficient data
    """
    if not raw_rows:
        return None

    # Extract timestamp from raw data
    timestamp = raw_rows[0].get("timestamp")
    if not timestamp:
        return None

    # Aggregate raw rows
    total_ce_oi = 0.0
    total_pe_oi = 0.0
    total_ce_volume = 0.0
    total_pe_volume = 0.0
    spot_prices = []
    atm_strikes = []
    ce_ivs = []
    pe_ivs = []

    for row in raw_rows:
        ce_oi = utils.safe_float(row.get("ce_oi"), 0.0)
        pe_oi = utils.safe_float(row.get("pe_oi"), 0.0)
        total_ce_oi += ce_oi
        total_pe_oi += pe_oi

        ce_vol = utils.safe_float(row.get("ce_volume"), 0.0)
        pe_vol = utils.safe_float(row.get("pe_volume"), 0.0)
        total_ce_volume += ce_vol
        total_pe_volume += pe_vol

        spot = utils.safe_float(row.get("underlying_spot_price"))
        if spot is not None:
            spot_prices.append(spot)

        atm = utils.safe_float(row.get("atm_strike"))
        if atm is not None:
            atm_strikes.append(atm)

        ce_iv = utils.safe_float(row.get("ce_iv"))
        if ce_iv is not None:
            ce_ivs.append(ce_iv)

        pe_iv = utils.safe_float(row.get("pe_iv"))
        if pe_iv is not None:
            pe_ivs.append(pe_iv)

    spot_price = sum(spot_prices) / len(spot_prices) if spot_prices else None
    atm_strike = sum(atm_strikes) / len(atm_strikes) if atm_strikes else None

    # PCR
    pcr = (total_pe_oi / total_ce_oi) if total_ce_oi > 0 else None

    # Volume PCR
    volume_pcr = (total_pe_volume / total_ce_volume) if total_ce_volume > 0 else None

    # IV averages
    ce_iv_avg = sum(ce_ivs) / len(ce_ivs) if ce_ivs else None
    pe_iv_avg = sum(pe_ivs) / len(pe_ivs) if pe_ivs else None

    # OI change from baseline (post_settlement)
    ce_oi_change = None
    pe_oi_change = None
    if baseline_rows:
        baseline_ce_oi = sum(utils.safe_float(r.get("ce_oi"), 0.0) for r in baseline_rows)
        baseline_pe_oi = sum(utils.safe_float(r.get("pe_oi"), 0.0) for r in baseline_rows)
        ce_oi_change = total_ce_oi - baseline_ce_oi
        pe_oi_change = total_pe_oi - baseline_pe_oi

    # Cumulative OI change (from day start = first tick of the day)
    # We compute this from the first snapshot of the day
    ce_oi_cumm_change = None
    pe_oi_cumm_change = None
    first_tick = _get_first_tick_oi(instrument, date)
    if first_tick:
        ce_oi_cumm_change = total_ce_oi - first_tick["ce_oi"]
        pe_oi_cumm_change = total_pe_oi - first_tick["pe_oi"]

    # OI Difference = PE cumm - CE cumm
    oi_difference = None
    if pe_oi_cumm_change is not None and ce_oi_cumm_change is not None:
        oi_difference = pe_oi_cumm_change - ce_oi_cumm_change

    # Delta PCR = PE change / CE change (from baseline)
    delta_pcr = None
    if ce_oi_change is not None and ce_oi_change > 0 and pe_oi_change is not None:
        delta_pcr = pe_oi_change / ce_oi_change

    # Signed PCR (directional: > 1 bullish, < 1 bearish)
    signed_pcr = None
    if ce_oi_change is not None and pe_oi_change is not None:
        if abs(ce_oi_change) > 0:
            signed_pcr = pe_oi_change / abs(ce_oi_change)

    # Signal logic: BUY when oi_difference crosses from negative to positive
    # SELL when crosses from positive to negative
    signal = None
    crossover = 0
    if prev_tick and oi_difference is not None:
        prev_oi_diff = utils.safe_float(prev_tick.get("oi_difference"))
        if prev_oi_diff is not None:
            if prev_oi_diff <= 0 and oi_difference > 0:
                signal = "BUY"
                crossover = 1
            elif prev_oi_diff >= 0 and oi_difference < 0:
                signal = "SELL"
                crossover = 1

    return {
        "timestamp": timestamp,
        "instrument": instrument,
        "spot_price": spot_price,
        "atm_strike": atm_strike,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "pcr": pcr,
        "ce_oi_change": ce_oi_change,
        "pe_oi_change": pe_oi_change,
        "ce_oi_cumm_change": ce_oi_cumm_change,
        "pe_oi_cumm_change": pe_oi_cumm_change,
        "oi_difference": oi_difference,
        "delta_pcr": delta_pcr,
        "signed_pcr": signed_pcr,
        "volume_pcr": volume_pcr,
        "ce_volume": total_ce_volume,
        "pe_volume": total_pe_volume,
        "ce_iv_avg": ce_iv_avg,
        "pe_iv_avg": pe_iv_avg,
        "signal": signal,
        "crossover": crossover,
    }


def _get_first_tick_oi(instrument: str, date: str) -> dict[str, float] | None:
    """Get the first tick's total OI for the day (for cumulative change calc)."""
    with data_processor.connect() as conn:
        row = conn.execute(
            """
            SELECT
                SUM(COALESCE(ce_oi, 0)) AS ce_oi,
                SUM(COALESCE(pe_oi, 0)) AS pe_oi
            FROM oi_snapshots
            WHERE instrument = ?
              AND timestamp = (
                SELECT MIN(timestamp) FROM oi_snapshots
                WHERE instrument = ? AND substr(timestamp, 1, 10) = ?
              )
            """,
            (instrument, instrument, date),
        ).fetchone()
        if row and row["ce_oi"] is not None:
            return {"ce_oi": float(row["ce_oi"]), "pe_oi": float(row["pe_oi"])}
    return None


def get_latest_raw_snapshot(instrument: str, date: str) -> list[dict[str, Any]]:
    """Get the most recent raw snapshot rows for an instrument today."""
    with data_processor.connect() as conn:
        latest_ts = conn.execute(
            """
            SELECT MAX(timestamp) AS ts
            FROM oi_snapshots
            WHERE instrument = ? AND substr(timestamp, 1, 10) = ?
            """,
            (instrument, date),
        ).fetchone()
        if not latest_ts or not latest_ts["ts"]:
            return []
        rows = conn.execute(
            """
            SELECT * FROM oi_snapshots
            WHERE instrument = ? AND timestamp = ?
            """,
            (instrument, latest_ts["ts"]),
        ).fetchall()
        return [dict(r) for r in rows]


def get_baseline_rows(instrument: str, date: str, baseline_type: str = "post_settlement") -> list[dict[str, Any]]:
    """Get baseline rows for change calculations."""
    with data_processor.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM daily_baselines
            WHERE instrument = ? AND date = ? AND baseline_type = ?
            """,
            (instrument, date, baseline_type),
        ).fetchall()
        return [dict(r) for r in rows]


def get_prev_computed_tick(instrument: str, date: str) -> dict[str, Any] | None:
    """Get the most recent computed tick for signal/crossover detection."""
    with data_processor.connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM computed_ticks
            WHERE instrument = ? AND substr(timestamp, 1, 10) = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (instrument, date),
        ).fetchone()
        return dict(row) if row else None


def save_computed_tick(tick: dict[str, Any]) -> bool:
    """Upsert a computed tick into the computed_ticks table.

    Returns True if the tick was saved successfully.
    """
    if not tick:
        return False

    columns = [
        "timestamp", "instrument", "spot_price", "atm_strike",
        "total_ce_oi", "total_pe_oi", "pcr",
        "ce_oi_change", "pe_oi_change",
        "ce_oi_cumm_change", "pe_oi_cumm_change",
        "oi_difference", "delta_pcr", "signed_pcr",
        "volume_pcr", "ce_volume", "pe_volume",
        "ce_iv_avg", "pe_iv_avg",
        "signal", "crossover",
    ]

    values = tuple(tick.get(col) for col in columns)
    col_list = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)

    with data_processor.connect() as conn:
        conn.execute(
            f"""
            INSERT OR REPLACE INTO computed_ticks ({col_list})
            VALUES ({placeholders})
            """,
            values,
        )
    logger.info(
        "Computed tick saved: %s/%s pcr=%.4f oi_diff=%s signal=%s",
        tick.get("instrument"),
        tick.get("timestamp"),
        tick.get("pcr") or 0,
        tick.get("oi_difference"),
        tick.get("signal"),
    )
    return True


def run_compute_cycle(date: str | None = None) -> dict[str, Any]:
    """Run a full compute cycle for all instruments.

    This is called at exact minute boundaries. It reads the latest raw
    snapshot data, computes all metrics, and saves to computed_ticks.
    """
    today = date or utils.today_ist()
    results: dict[str, Any] = {}

    for instrument in utils.instrument_names():
        try:
            raw_rows = get_latest_raw_snapshot(instrument, today)
            if not raw_rows:
                results[instrument] = {"status": "no_data"}
                continue

            baseline_rows = get_baseline_rows(instrument, today)
            prev_tick = get_prev_computed_tick(instrument, today)

            tick = compute_tick(
                instrument=instrument,
                date=today,
                raw_rows=raw_rows,
                baseline_rows=baseline_rows,
                prev_tick=prev_tick,
            )

            if tick:
                saved = save_computed_tick(tick)
                results[instrument] = {
                    "status": "saved" if saved else "failed",
                    "timestamp": tick.get("timestamp"),
                    "pcr": tick.get("pcr"),
                    "signal": tick.get("signal"),
                }
            else:
                results[instrument] = {"status": "compute_failed"}

        except Exception as exc:
            logger.exception("Compute cycle failed for %s", instrument)
            results[instrument] = {"status": "error", "error": str(exc)}

    logger.info("Compute cycle complete: %s", results)
    return results


def get_computed_ticks(instrument: str, date: str) -> list[dict[str, Any]]:
    """Retrieve all computed ticks for an instrument on a given date."""
    with data_processor.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM computed_ticks
            WHERE instrument = ? AND substr(timestamp, 1, 10) = ?
            ORDER BY timestamp
            """,
            (instrument, date),
        ).fetchall()
        return [dict(r) for r in rows]
