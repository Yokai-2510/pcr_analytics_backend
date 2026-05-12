"""Dashboard summary, flexible data browsing, and recent-events helpers.

This module powers the redesigned frontend:
  - /api/dashboard/summary       -> one-shot card data for all instruments
  - /api/data/columns            -> available columns for the data browser
  - /api/data/distinct/...       -> distinct values for filter dropdowns
  - /api/data/query              -> flexible row query over oi_snapshots
  - /api/events/recent           -> recent log/event entries from app.log
"""

from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

import data_processor
import utilities as utils

# ── Column catalogue for the data browser ──────────────────────────────────

_NUMERIC_COLUMNS = {
    column for column in data_processor.SNAPSHOT_COLUMNS
    if column not in data_processor.TEXT_COLUMNS
}

_FILTER_OPS = {
    "string": ["eq", "ne", "in", "contains"],
    "number": ["eq", "ne", "gt", "gte", "lt", "lte", "between", "in"],
}

COLUMN_GROUPS: list[tuple[str, list[str]]] = [
    ("identity", ["timestamp", "instrument", "expiry", "strike", "underlying_key"]),
    ("price", ["underlying_spot_price", "atm_strike", "ce_ltp", "pe_ltp",
               "ce_close_price", "pe_close_price",
               "ce_bid_price", "ce_ask_price", "pe_bid_price", "pe_ask_price"]),
    ("ratio", ["pcr"]),
    ("open_interest", ["ce_oi", "pe_oi", "ce_prev_oi", "pe_prev_oi"]),
    ("volume", ["ce_volume", "pe_volume", "ce_bid_qty", "ce_ask_qty",
                "pe_bid_qty", "pe_ask_qty"]),
    ("greeks", ["ce_iv", "pe_iv", "ce_delta", "pe_delta", "ce_gamma", "pe_gamma",
                "ce_theta", "pe_theta", "ce_vega", "pe_vega", "ce_pop", "pe_pop"]),
    ("instrument_keys", ["ce_instrument_key", "pe_instrument_key"]),
]

COLUMN_LABELS: dict[str, str] = {
    "timestamp": "Timestamp",
    "instrument": "Instrument",
    "expiry": "Expiry",
    "strike": "Strike",
    "underlying_key": "Underlying Key",
    "underlying_spot_price": "Spot",
    "atm_strike": "ATM Strike",
    "pcr": "PCR",
    "ce_ltp": "Call LTP",
    "pe_ltp": "Put LTP",
    "ce_close_price": "Call Close",
    "pe_close_price": "Put Close",
    "ce_bid_price": "Call Bid",
    "ce_ask_price": "Call Ask",
    "pe_bid_price": "Put Bid",
    "pe_ask_price": "Put Ask",
    "ce_oi": "Call OI",
    "pe_oi": "Put OI",
    "ce_prev_oi": "Call Prev OI",
    "pe_prev_oi": "Put Prev OI",
    "ce_volume": "Call Volume",
    "pe_volume": "Put Volume",
    "ce_bid_qty": "Call Bid Qty",
    "ce_ask_qty": "Call Ask Qty",
    "pe_bid_qty": "Put Bid Qty",
    "pe_ask_qty": "Put Ask Qty",
    "ce_iv": "Call IV",
    "pe_iv": "Put IV",
    "ce_delta": "Call Delta",
    "pe_delta": "Put Delta",
    "ce_gamma": "Call Gamma",
    "pe_gamma": "Put Gamma",
    "ce_theta": "Call Theta",
    "pe_theta": "Put Theta",
    "ce_vega": "Call Vega",
    "pe_vega": "Put Vega",
    "ce_pop": "Call PoP",
    "pe_pop": "Put PoP",
    "ce_instrument_key": "Call Instrument Key",
    "pe_instrument_key": "Put Instrument Key",
}


def _column_type(column: str) -> str:
    return "string" if column in data_processor.TEXT_COLUMNS else "number"


def column_catalog() -> dict[str, Any]:
    """Return columns with type/group/label so the frontend can render filter UIs."""
    columns = []
    for group, cols in COLUMN_GROUPS:
        for col in cols:
            if col not in data_processor.SNAPSHOT_COLUMNS:
                continue
            ctype = _column_type(col)
            columns.append({
                "id": col,
                "label": COLUMN_LABELS.get(col, col),
                "group": group,
                "type": ctype,
                "operators": _FILTER_OPS[ctype],
                "filterable": True,
                "sortable": True,
            })
    return {
        "columns": columns,
        "groups": [g for g, _ in COLUMN_GROUPS],
        "default_columns": [
            "timestamp", "instrument", "strike", "underlying_spot_price",
            "atm_strike", "pcr", "ce_oi", "pe_oi", "ce_volume", "pe_volume",
        ],
    }


# ── Distinct values for filter dropdowns ──────────────────────────────────

def distinct_values(
    column: str,
    *,
    instrument: str | None = None,
    date: str | None = None,
    limit: int = 500,
) -> list[Any]:
    if column not in data_processor.SNAPSHOT_COLUMNS:
        raise ValueError(f"unknown column: {column}")
    where: list[str] = []
    params: list[Any] = []
    if instrument:
        where.append("instrument = ?")
        params.append(utils.normalize_instrument_name(instrument))
    if date:
        where.append("substr(timestamp, 1, 10) = ?")
        params.append(date)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    query = (
        f"SELECT DISTINCT {column} AS value FROM oi_snapshots "
        f"{clause} ORDER BY value LIMIT {int(limit)}"
    )
    with data_processor.connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [row["value"] for row in rows if row["value"] is not None]


# ── Flexible data query ───────────────────────────────────────────────────

_OP_TO_SQL = {
    "eq": "{col} = ?",
    "ne": "{col} != ?",
    "gt": "{col} > ?",
    "gte": "{col} >= ?",
    "lt": "{col} < ?",
    "lte": "{col} <= ?",
    "contains": "{col} LIKE ?",
}


def _validate_columns(columns: list[str] | None) -> list[str]:
    if not columns:
        return list(data_processor.SNAPSHOT_COLUMNS)
    invalid = [c for c in columns if c not in data_processor.SNAPSHOT_COLUMNS]
    if invalid:
        raise ValueError(f"unknown column(s): {', '.join(invalid)}")
    # Always include timestamp and instrument so the table is usable.
    seen: list[str] = []
    for c in ("timestamp", "instrument", *columns):
        if c not in seen and c in data_processor.SNAPSHOT_COLUMNS:
            seen.append(c)
    return seen


def _build_where(filters: list[dict[str, Any]] | None) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    for item in filters or []:
        col = item.get("column")
        op = (item.get("op") or "eq").lower()
        value = item.get("value")
        if col not in data_processor.SNAPSHOT_COLUMNS:
            raise ValueError(f"unknown filter column: {col}")
        if op == "in":
            values = value if isinstance(value, list) else [value]
            if not values:
                continue
            placeholders = ", ".join("?" for _ in values)
            where.append(f"{col} IN ({placeholders})")
            params.extend(values)
            continue
        if op == "between":
            if not (isinstance(value, list) and len(value) == 2):
                raise ValueError("'between' expects [low, high]")
            where.append(f"{col} BETWEEN ? AND ?")
            params.extend(value)
            continue
        if op == "contains":
            where.append(_OP_TO_SQL[op].format(col=col))
            params.append(f"%{value}%")
            continue
        if op not in _OP_TO_SQL:
            raise ValueError(f"unsupported operator: {op}")
        where.append(_OP_TO_SQL[op].format(col=col))
        params.append(value)
    return where, params


def query_data(payload: dict[str, Any]) -> dict[str, Any]:
    """Run a paginated, filterable query against oi_snapshots."""
    instrument = payload.get("instrument")
    date = payload.get("date")
    time_from = payload.get("time_from")
    time_to = payload.get("time_to")
    columns = _validate_columns(payload.get("columns"))
    filters = payload.get("filters") or []
    sort = payload.get("sort") or [{"column": "timestamp", "dir": "desc"}]
    page = max(1, int(payload.get("page") or 1))
    page_size = min(500, max(1, int(payload.get("page_size") or 100)))

    where, params = _build_where(filters)
    if instrument:
        where.append("instrument = ?")
        params.append(utils.normalize_instrument_name(instrument))
    if date:
        where.append("substr(timestamp, 1, 10) = ?")
        params.append(date)
    if time_from:
        where.append("timestamp >= ?")
        params.append(time_from)
    if time_to:
        where.append("timestamp <= ?")
        params.append(time_to)

    order_parts: list[str] = []
    for spec in sort:
        col = spec.get("column")
        if col not in data_processor.SNAPSHOT_COLUMNS:
            continue
        direction = "DESC" if str(spec.get("dir", "")).lower() == "desc" else "ASC"
        order_parts.append(f"{col} {direction}")
    order_clause = ", ".join(order_parts) if order_parts else "timestamp DESC"

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    select_columns = ", ".join(columns)

    offset = (page - 1) * page_size
    base_query = f"FROM oi_snapshots {where_clause}"

    with data_processor.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS n {base_query}", tuple(params)).fetchone()["n"]
        rows = conn.execute(
            f"SELECT {select_columns} {base_query} ORDER BY {order_clause} LIMIT ? OFFSET ?",
            (*params, page_size, offset),
        ).fetchall()

    return {
        "rows": [dict(row) for row in rows],
        "columns": columns,
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "pages": (int(total) + page_size - 1) // page_size if total else 0,
    }


# ── Dashboard summary ─────────────────────────────────────────────────────

def _aggregate_latest_for(instrument: str, date: str) -> dict[str, Any] | None:
    with data_processor.connect() as conn:
        latest = conn.execute(
            """
            SELECT MAX(timestamp) AS ts
            FROM oi_snapshots
            WHERE instrument = ? AND substr(timestamp, 1, 10) = ?
            """,
            (instrument, date),
        ).fetchone()
        ts = latest and latest["ts"]
        if not ts:
            return None

        agg = conn.execute(
            """
            SELECT
                MAX(timestamp) AS timestamp,
                AVG(underlying_spot_price) AS spot,
                AVG(atm_strike) AS atm_strike,
                SUM(COALESCE(ce_oi, 0)) AS total_ce_oi,
                SUM(COALESCE(pe_oi, 0)) AS total_pe_oi,
                SUM(COALESCE(ce_volume, 0)) AS total_ce_volume,
                SUM(COALESCE(pe_volume, 0)) AS total_pe_volume,
                CASE WHEN SUM(COALESCE(ce_oi, 0)) > 0
                     THEN SUM(COALESCE(pe_oi, 0)) / SUM(COALESCE(ce_oi, 0))
                     ELSE NULL END AS pcr,
                COUNT(DISTINCT strike) AS strikes,
                COUNT(*) AS rows
            FROM oi_snapshots
            WHERE instrument = ? AND timestamp = ?
            """,
            (instrument, ts),
        ).fetchone()

        first = conn.execute(
            """
            SELECT AVG(underlying_spot_price) AS spot
            FROM oi_snapshots
            WHERE instrument = ?
              AND timestamp = (
                SELECT MIN(timestamp) FROM oi_snapshots
                WHERE instrument = ? AND substr(timestamp, 1, 10) = ?
              )
            """,
            (instrument, instrument, date),
        ).fetchone()

        baseline = conn.execute(
            """
            SELECT
                SUM(COALESCE(ce_oi, 0)) AS ce_oi,
                SUM(COALESCE(pe_oi, 0)) AS pe_oi
            FROM daily_baselines
            WHERE date = ? AND baseline_type = 'post_settlement' AND instrument = ?
            """,
            (date, instrument),
        ).fetchone()

    open_spot = first and first["spot"]
    spot = agg and agg["spot"]
    change_abs = (spot - open_spot) if spot is not None and open_spot is not None else None
    change_pct = (
        (change_abs / open_spot * 100.0)
        if change_abs is not None and open_spot
        else None
    )
    base_ce = baseline and baseline["ce_oi"]
    base_pe = baseline and baseline["pe_oi"]

    return {
        "timestamp": ts,
        "spot": spot,
        "open_spot": open_spot,
        "change_abs": change_abs,
        "change_pct": change_pct,
        "atm_strike": agg["atm_strike"],
        "pcr": agg["pcr"],
        "total_ce_oi": agg["total_ce_oi"],
        "total_pe_oi": agg["total_pe_oi"],
        "total_ce_volume": agg["total_ce_volume"],
        "total_pe_volume": agg["total_pe_volume"],
        "ce_oi_change": (agg["total_ce_oi"] - base_ce) if base_ce is not None else None,
        "pe_oi_change": (agg["total_pe_oi"] - base_pe) if base_pe is not None else None,
        "strikes": agg["strikes"],
        "snapshot_rows": agg["rows"],
        "baseline_ce_oi": base_ce,
        "baseline_pe_oi": base_pe,
    }


def _spark_series(instrument: str, date: str, *, limit: int = 60) -> dict[str, list[Any]]:
    with data_processor.connect() as conn:
        rows = conn.execute(
            """
            SELECT timestamp,
                   AVG(underlying_spot_price) AS spot,
                   CASE WHEN SUM(COALESCE(ce_oi, 0)) > 0
                        THEN SUM(COALESCE(pe_oi, 0)) / SUM(COALESCE(ce_oi, 0))
                        ELSE NULL END AS pcr
            FROM oi_snapshots
            WHERE instrument = ? AND substr(timestamp, 1, 10) = ?
            GROUP BY timestamp
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (instrument, date, int(limit)),
        ).fetchall()
    rows = list(reversed(rows))
    return {
        "timestamps": [row["timestamp"] for row in rows],
        "spot": [row["spot"] for row in rows],
        "pcr": [row["pcr"] for row in rows],
    }


def _market_sentiment(pcr: float | None) -> dict[str, Any]:
    if pcr is None:
        return {"label": "neutral", "score": 0, "tone": "neutral"}
    if pcr >= 1.3:
        return {"label": "strongly bullish", "score": min(100, int((pcr - 1) * 100)), "tone": "bull"}
    if pcr >= 1.05:
        return {"label": "bullish", "score": int((pcr - 1) * 100), "tone": "bull"}
    if pcr <= 0.7:
        return {"label": "strongly bearish", "score": min(100, int((1 - pcr) * 100)), "tone": "bear"}
    if pcr <= 0.95:
        return {"label": "bearish", "score": int((1 - pcr) * 100), "tone": "bear"}
    return {"label": "neutral", "score": 0, "tone": "neutral"}


def dashboard_summary(date: str | None = None, *, spark_points: int = 60) -> dict[str, Any]:
    today = (date or utils.today_ist())[:10]
    instruments = utils.instrument_names()

    summary: list[dict[str, Any]] = []
    for name in instruments:
        cfg = utils.instrument_config(name)
        latest = _aggregate_latest_for(name, today)
        spark = _spark_series(name, today, limit=spark_points)
        if latest is None:
            summary.append({
                "instrument": name,
                "label": cfg.get("name") or name.title(),
                "instrument_key": cfg.get("instrument_key"),
                "strike_step": cfg.get("strike_step"),
                "strike_count": cfg.get("strike_count"),
                "available": False,
                "spark": spark,
            })
            continue
        sentiment = _market_sentiment(latest.get("pcr"))
        summary.append({
            "instrument": name,
            "label": cfg.get("name") or name.title(),
            "instrument_key": cfg.get("instrument_key"),
            "strike_step": cfg.get("strike_step"),
            "strike_count": cfg.get("strike_count"),
            "available": True,
            "sentiment": sentiment,
            "spark": spark,
            **latest,
        })

    status = utils.service_status()
    totals_ce = sum((row.get("total_ce_oi") or 0) for row in summary if row.get("available"))
    totals_pe = sum((row.get("total_pe_oi") or 0) for row in summary if row.get("available"))
    portfolio_pcr = (totals_pe / totals_ce) if totals_ce else None

    return {
        "date": today,
        "generated_at": utils.iso_now(),
        "instruments": summary,
        "totals": {
            "total_ce_oi": totals_ce,
            "total_pe_oi": totals_pe,
            "portfolio_pcr": portfolio_pcr,
            "sentiment": _market_sentiment(portfolio_pcr),
            "available_instruments": sum(1 for row in summary if row.get("available")),
        },
        "service": {
            "market_state": status.get("market_state"),
            "last_fetch": status.get("last_fetch"),
            "next_fetch": status.get("next_fetch"),
            "last_error": status.get("last_error"),
            "running": status.get("running"),
            "collector_running": status.get("collector_running"),
            "api_running": status.get("api_running"),
        },
    }


# ── Recent events (parsed from app.log) ───────────────────────────────────

_LOG_LINE = re.compile(
    r"^\[(?P<time>[^\]]+)\]\s+\[(?P<level>[A-Z]+)\]\s+\[(?P<logger>[^\]]+)\]\s+(?P<message>.*)$"
)


def recent_events(*, limit: int = 200, level: str | None = None) -> list[dict[str, Any]]:
    log_path = utils.logs_dir() / "app.log"
    if not log_path.exists():
        return []
    needle = (level or "").upper().strip() or None
    # Tail the file: read up to ~512KB from the end.
    with log_path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 512 * 1024))
        chunk = f.read().decode("utf-8", "replace")
    lines = chunk.splitlines()
    events: list[dict[str, Any]] = []
    for line in lines:
        match = _LOG_LINE.match(line)
        if not match:
            if events:
                events[-1]["message"] += "\n" + line
            continue
        ev_level = match.group("level").upper()
        if needle and ev_level != needle:
            continue
        events.append({
            "timestamp": match.group("time"),
            "level": ev_level,
            "logger": match.group("logger"),
            "message": match.group("message"),
        })
    return events[-int(limit):][::-1]
