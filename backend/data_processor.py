"""Raw CSV logging, ATM filtering, SQLite writes, and query helpers."""

from __future__ import annotations

import csv
import logging
import sqlite3
from typing import Any

import utilities as utils

logger = logging.getLogger(__name__)

CSV_COLUMNS = [
    "timestamp",
    "instrument",
    "expiry",
    "underlying_key",
    "underlying_spot_price",
    "strike",
    "strike_pcr",
    "option_type",
    "option_instrument_key",
    "ltp",
    "oi",
    "prev_oi",
    "volume",
    "close_price",
    "bid_price",
    "bid_qty",
    "ask_price",
    "ask_qty",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "pop",
]

OPTION_FIELD_SUFFIXES = [
    "instrument_key",
    "ltp",
    "oi",
    "prev_oi",
    "volume",
    "close_price",
    "bid_price",
    "bid_qty",
    "ask_price",
    "ask_qty",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "pop",
]

SNAPSHOT_COLUMNS = [
    "timestamp",
    "instrument",
    "expiry",
    "strike",
    "underlying_key",
    "underlying_spot_price",
    "atm_strike",
    "pcr",
    *[f"ce_{suffix}" for suffix in OPTION_FIELD_SUFFIXES],
    *[f"pe_{suffix}" for suffix in OPTION_FIELD_SUFFIXES],
]

BASELINE_COLUMNS = [
    "date",
    "baseline_type",
    "snapshot_timestamp",
    *[column for column in SNAPSHOT_COLUMNS if column != "timestamp"],
]

TEXT_COLUMNS = {
    "timestamp",
    "date",
    "baseline_type",
    "snapshot_timestamp",
    "instrument",
    "expiry",
    "underlying_key",
    "ce_instrument_key",
    "pe_instrument_key",
}

NOT_NULL_COLUMNS = {
    "timestamp",
    "date",
    "baseline_type",
    "instrument",
    "expiry",
    "strike",
}


def connect() -> sqlite3.Connection:
    utils.db_path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(utils.db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _column_type(column: str) -> str:
    return "TEXT" if column in TEXT_COLUMNS else "REAL"


def _column_def(column: str, *, allow_not_null: bool) -> str:
    suffix = " NOT NULL" if allow_not_null and column in NOT_NULL_COLUMNS else ""
    return f"{column} {_column_type(column)}{suffix}"


def _create_columns_sql(columns: list[str]) -> str:
    column_defs = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
    column_defs.extend(_column_def(column, allow_not_null=True) for column in columns)
    return ",\n                ".join(column_defs)


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: list[str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for column in columns:
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {_column_def(column, allow_not_null=False)}")


def initialize_storage() -> None:
    with connect() as conn:
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS oi_snapshots (
                {_create_columns_sql(SNAPSHOT_COLUMNS)}
            );

            CREATE INDEX IF NOT EXISTS idx_oi_snapshots_lookup
                ON oi_snapshots (instrument, timestamp, strike);

            CREATE TABLE IF NOT EXISTS daily_baselines (
                {_create_columns_sql(BASELINE_COLUMNS)},
                UNIQUE(date, baseline_type, instrument, expiry, strike)
            );

            CREATE INDEX IF NOT EXISTS idx_daily_baselines_lookup
                ON daily_baselines (date, baseline_type, instrument, expiry, strike);

            CREATE TABLE IF NOT EXISTS chart_configs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chart_configs_updated
                ON chart_configs (updated_at);
            """
        )
        _ensure_columns(conn, "oi_snapshots", SNAPSHOT_COLUMNS)
        _ensure_columns(conn, "daily_baselines", BASELINE_COLUMNS)
    logger.info("SQLite initialized at %s", utils.db_path())


def _csv_path(instrument: str, timestamp: str) -> Any:
    return utils.logs_dir() / f"{timestamp[:10]}_{instrument}.csv"


def append_raw_csv(raw_data: dict[str, dict[str, Any] | None]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for instrument, payload in raw_data.items():
        if not payload:
            counts[instrument] = 0
            continue

        timestamp = str(payload["timestamp"])
        expiry = str(payload["expiry"])
        rows: list[dict[str, Any]] = []
        for strike_row in payload.get("strikes") or []:
            if not isinstance(strike_row, dict):
                continue
            rows.append(
                utils.format_option_csv_row(
                    timestamp=timestamp,
                    instrument=instrument,
                    expiry=expiry,
                    strike_row=strike_row,
                    option_type="CE",
                )
            )
            rows.append(
                utils.format_option_csv_row(
                    timestamp=timestamp,
                    instrument=instrument,
                    expiry=expiry,
                    strike_row=strike_row,
                    option_type="PE",
                )
            )

        if rows:
            path = _csv_path(instrument, timestamp)
            path.parent.mkdir(parents=True, exist_ok=True)
            write_header = not path.exists() or path.stat().st_size == 0
            with path.open("a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                if write_header:
                    writer.writeheader()
                writer.writerows(rows)
        counts[instrument] = len(rows)
    return counts


def build_filtered_snapshots(
    raw_data: dict[str, dict[str, Any] | None],
) -> dict[str, dict[str, Any] | None]:
    filtered: dict[str, dict[str, Any] | None] = {}
    for instrument, payload in raw_data.items():
        if not payload:
            filtered[instrument] = None
            continue
        filtered_payload = utils.filter_atm_window(payload)
        if not filtered_payload:
            logger.error("Missing spot price for %s; skipping SQLite snapshot", instrument)
        filtered[instrument] = filtered_payload
    return filtered


def _option_db_values(strike_row: dict[str, Any], option_type: str) -> list[Any]:
    payload = utils.option_payload(strike_row, option_type)
    market_data = utils.option_market_data(strike_row, option_type)
    greeks = utils.option_greeks(strike_row, option_type)
    return [
        payload.get("instrument_key"),
        utils.safe_float(market_data.get("ltp")),
        utils.safe_float(market_data.get("oi")),
        utils.safe_float(market_data.get("prev_oi")),
        utils.safe_float(market_data.get("volume")),
        utils.safe_float(market_data.get("close_price")),
        utils.safe_float(market_data.get("bid_price")),
        utils.safe_float(market_data.get("bid_qty")),
        utils.safe_float(market_data.get("ask_price")),
        utils.safe_float(market_data.get("ask_qty")),
        utils.safe_float(greeks.get("iv")),
        utils.safe_float(greeks.get("delta")),
        utils.safe_float(greeks.get("gamma")),
        utils.safe_float(greeks.get("theta")),
        utils.safe_float(greeks.get("vega")),
        utils.safe_float(greeks.get("pop")),
    ]


def _snapshot_db_row(instrument: str, payload: dict[str, Any], strike_row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        payload["timestamp"],
        instrument,
        strike_row.get("expiry") or payload["expiry"],
        utils.safe_float(strike_row.get("strike_price")),
        strike_row.get("underlying_key"),
        utils.safe_float(strike_row.get("underlying_spot_price")),
        utils.safe_float(payload.get("atm_strike")),
        utils.safe_float(strike_row.get("pcr")),
        *_option_db_values(strike_row, "CE"),
        *_option_db_values(strike_row, "PE"),
    )


def write_snapshot(filtered_data: dict[str, dict[str, Any] | None]) -> dict[str, int]:
    rows: list[tuple[Any, ...]] = []
    counts: dict[str, int] = {}

    for instrument, payload in filtered_data.items():
        if not payload:
            counts[instrument] = 0
            continue
        row_count = 0
        for strike_row in payload.get("strikes") or []:
            rows.append(_snapshot_db_row(instrument, payload, strike_row))
            row_count += 1
        counts[instrument] = row_count

    if rows:
        columns = ", ".join(SNAPSHOT_COLUMNS)
        placeholders = ", ".join("?" for _ in SNAPSHOT_COLUMNS)
        with connect() as conn:
            conn.executemany(
                f"INSERT INTO oi_snapshots ({columns}) VALUES ({placeholders})",
                rows,
            )
    return counts


def persist_market_data(raw_data: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    csv_counts = append_raw_csv(raw_data)
    filtered = build_filtered_snapshots(raw_data)
    snapshot_counts = write_snapshot(filtered)
    summary = {
        "csv_counts": csv_counts,
        "snapshot_counts": snapshot_counts,
        "filtered_counts": {
            instrument: len(payload.get("strikes") or []) if payload else 0
            for instrument, payload in filtered.items()
        },
    }
    logger.info("Persisted market data: %s", summary)
    return summary


def save_baseline(baseline_type: str, date: str | None = None) -> dict[str, int]:
    if baseline_type not in {"post_settlement", "prev_close"}:
        raise ValueError("baseline_type must be post_settlement or prev_close")

    baseline_date = date or utils.today_ist()
    counts: dict[str, int] = {}
    with connect() as conn:
        for instrument in utils.instrument_names():
            if baseline_type == "post_settlement":
                existing = conn.execute(
                    """
                    SELECT COUNT(*) AS rows
                    FROM daily_baselines
                    WHERE date = ? AND baseline_type = ? AND instrument = ?
                    """,
                    (baseline_date, baseline_type, instrument),
                ).fetchone()["rows"]
                if existing:
                    counts[instrument] = int(existing)
                    logger.info(
                        "Baseline %s/%s already exists for %s; preserving it",
                        baseline_type,
                        instrument,
                        baseline_date,
                    )
                    continue

            latest = conn.execute(
                """
                SELECT MAX(timestamp) AS timestamp
                FROM oi_snapshots
                WHERE instrument = ? AND substr(timestamp, 1, 10) = ?
                """,
                (instrument, baseline_date),
            ).fetchone()["timestamp"]
            if not latest:
                counts[instrument] = 0
                logger.warning(
                    "No snapshot available for baseline %s/%s on %s",
                    baseline_type,
                    instrument,
                    baseline_date,
                )
                continue

            snapshot_columns = ", ".join(SNAPSHOT_COLUMNS)
            rows = conn.execute(
                f"""
                SELECT {snapshot_columns}
                FROM oi_snapshots
                WHERE instrument = ? AND timestamp = ?
                """,
                (instrument, latest),
            ).fetchall()
            baseline_columns = ", ".join(BASELINE_COLUMNS)
            placeholders = ", ".join("?" for _ in BASELINE_COLUMNS)
            source_columns = [column for column in SNAPSHOT_COLUMNS if column != "timestamp"]
            conn.executemany(
                f"INSERT OR REPLACE INTO daily_baselines ({baseline_columns}) VALUES ({placeholders})",
                [
                    (
                        baseline_date,
                        baseline_type,
                        row["timestamp"],
                        *(row[column] for column in source_columns),
                    )
                    for row in rows
                ],
            )
            counts[instrument] = len(rows)
    logger.info("Baseline %s frozen for %s: %s", baseline_type, baseline_date, counts)
    return counts


def _query(query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with connect() as conn:
        return utils.row_dicts(conn.execute(query, params).fetchall())


def get_pcr_series(instrument: str, date: str) -> list[dict[str, Any]]:
    return _query(
        """
        SELECT
            timestamp,
            CASE WHEN SUM(COALESCE(ce_oi, 0)) > 0
                THEN SUM(COALESCE(pe_oi, 0)) / SUM(COALESCE(ce_oi, 0))
                ELSE NULL
            END AS pcr_value,
            AVG(underlying_spot_price) AS underlying_spot_price
        FROM oi_snapshots
        WHERE instrument = ? AND substr(timestamp, 1, 10) = ?
        GROUP BY timestamp
        ORDER BY timestamp
        """,
        (instrument, date),
    )


def get_total_oi_series(instrument: str, date: str) -> list[dict[str, Any]]:
    return _query(
        """
        SELECT
            timestamp,
            SUM(COALESCE(ce_oi, 0)) AS total_ce_oi,
            SUM(COALESCE(pe_oi, 0)) AS total_pe_oi,
            AVG(underlying_spot_price) AS underlying_spot_price
        FROM oi_snapshots
        WHERE instrument = ? AND substr(timestamp, 1, 10) = ?
        GROUP BY timestamp
        ORDER BY timestamp
        """,
        (instrument, date),
    )


def get_oi_change_series(instrument: str, date: str, baseline: str) -> list[dict[str, Any]]:
    return _query(
        """
        SELECT
            s.timestamp,
            SUM(COALESCE(s.ce_oi, 0) - COALESCE(b.ce_oi, 0)) AS ce_oi_change,
            SUM(COALESCE(s.pe_oi, 0) - COALESCE(b.pe_oi, 0)) AS pe_oi_change,
            AVG(s.underlying_spot_price) AS underlying_spot_price
        FROM oi_snapshots s
        LEFT JOIN daily_baselines b
            ON b.date = ?
            AND b.baseline_type = ?
            AND b.instrument = s.instrument
            AND b.expiry = s.expiry
            AND b.strike = s.strike
        WHERE s.instrument = ? AND substr(s.timestamp, 1, 10) = ?
        GROUP BY s.timestamp
        ORDER BY s.timestamp
        """,
        (date, baseline, instrument, date),
    )


def get_snapshots(instrument: str, date: str, strike: float | None = None) -> list[dict[str, Any]]:
    if strike is None:
        return _query(
            """
            SELECT *
            FROM oi_snapshots
            WHERE instrument = ? AND substr(timestamp, 1, 10) = ?
            ORDER BY timestamp, strike
            """,
            (instrument, date),
        )
    return _query(
        """
        SELECT *
        FROM oi_snapshots
        WHERE instrument = ? AND substr(timestamp, 1, 10) = ? AND strike = ?
        ORDER BY timestamp, strike
        """,
        (instrument, date, strike),
    )


def get_history_summary(instrument: str | None = None) -> list[dict[str, Any]]:
    params: tuple[Any, ...]
    where = ""
    if instrument:
        where = "WHERE instrument = ?"
        params = (instrument,)
    else:
        params = ()
    return _query(
        f"""
        SELECT
            instrument,
            substr(timestamp, 1, 10) AS date,
            MIN(timestamp) AS first_timestamp,
            MAX(timestamp) AS last_timestamp,
            COUNT(*) AS snapshot_rows,
            COUNT(DISTINCT timestamp) AS ticks,
            COUNT(DISTINCT strike) AS strikes
        FROM oi_snapshots
        {where}
        GROUP BY instrument, substr(timestamp, 1, 10)
        ORDER BY date DESC, instrument
        """,
        params,
    )
