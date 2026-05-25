"""Persistence for the trade subsystem.

Owns four tables — ``trade_configs``, ``orders``, ``positions``,
``order_audit`` — plus the read/write helpers the engine and API use.

All writes happen inside the worker process (engine thread); the API
process is read-only against these tables. Schema is initialised
idempotently and is safe to run on every worker start.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Iterable

import data_processor
import utilities as utils

logger = logging.getLogger(__name__)


# ── Schema ─────────────────────────────────────────────────────────────


_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS trade_configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        json_blob TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trade_configs_active ON trade_configs(active)",
    """
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_order_ref TEXT NOT NULL UNIQUE,
        broker_order_id TEXT,
        instrument TEXT NOT NULL,
        instrument_token TEXT NOT NULL,
        strike REAL NOT NULL,
        option_type TEXT NOT NULL,
        transaction_type TEXT NOT NULL,
        qty INTEGER NOT NULL,
        lots INTEGER NOT NULL,
        price REAL NOT NULL,
        status TEXT NOT NULL,
        intent TEXT NOT NULL,
        parent_position_id INTEGER,
        mode TEXT NOT NULL,
        signal_timestamp TEXT,
        placed_at TEXT NOT NULL,
        error TEXT,
        UNIQUE(instrument, signal_timestamp, intent)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_orders_placed_at ON orders(placed_at)",
    "CREATE INDEX IF NOT EXISTS idx_orders_intent ON orders(intent)",
    """
    CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instrument TEXT NOT NULL,
        instrument_token TEXT NOT NULL,
        strike REAL NOT NULL,
        option_type TEXT NOT NULL,
        qty INTEGER NOT NULL,
        lots INTEGER NOT NULL,
        entry_order_id INTEGER NOT NULL,
        exit_order_id INTEGER,
        entry_price REAL NOT NULL,
        exit_price REAL,
        entry_time TEXT NOT NULL,
        exit_time TEXT,
        status TEXT NOT NULL,
        high_watermark REAL,
        sl_price REAL,
        target_price REAL,
        exit_reason TEXT,
        mode TEXT NOT NULL,
        signal_timestamp TEXT,
        pnl REAL,
        ctx_oi_difference REAL,
        ctx_pcr REAL,
        ctx_ce_cumm REAL,
        ctx_pe_cumm REAL,
        ctx_margin REAL,
        manual_exit_requested INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status, instrument)",
    "CREATE INDEX IF NOT EXISTS idx_positions_entry_time ON positions(entry_time)",
    """
    CREATE TABLE IF NOT EXISTS order_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        instrument TEXT,
        client_order_ref TEXT,
        position_id INTEGER,
        gate TEXT,
        message TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_order_audit_ts ON order_audit(ts)",
    """
    CREATE TABLE IF NOT EXISTS daily_trade_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL UNIQUE,
        generated_at TEXT NOT NULL,
        json_blob TEXT NOT NULL
    )
    """,
]


def init_schema(conn: sqlite3.Connection) -> None:
    """Create trade tables if they don't exist. Idempotent."""
    for stmt in _SCHEMA:
        conn.execute(stmt)
    conn.commit()
    logger.info("trade.persistence: schema initialised")


# ── Config ─────────────────────────────────────────────────────────────


DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "paper",
    "auto_execute": False,
    "cooldown_minutes": 0,
    "instruments": ["nifty"],
    "strike_mode": "atm",
    "custom_strike": None,
    "lots": 1,
    "exit_on_counter_crossover": True,
    "stop_loss_enabled": True,
    "stop_loss_pct": 30,
    "trailing_sl_enabled": False,
    "trailing_sl_trigger_pct": 20,
    "trailing_sl_step_pct": 10,
    "target_enabled": True,
    "target_pct": 50,
    "time_exit_enabled": True,
    "time_exit_at": "15:15",
    "max_positions_per_day": 3,
}


def get_active_config() -> dict[str, Any]:
    """Return the currently active config merged onto defaults. Always returns
    a usable dict even if no config has been saved yet."""
    with data_processor.connect() as conn:
        row = conn.execute(
            "SELECT json_blob FROM trade_configs WHERE active = 1 LIMIT 1"
        ).fetchone()
    if not row:
        return dict(DEFAULT_CONFIG)
    try:
        saved = json.loads(row["json_blob"])
        if not isinstance(saved, dict):
            saved = {}
    except (ValueError, TypeError):
        saved = {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(saved)
    return merged


def save_config(new_blob: dict[str, Any]) -> dict[str, Any]:
    """Insert a new config row and mark it active, deactivating any previous
    row. Returns the persisted (defaults-merged) config."""
    merged = dict(DEFAULT_CONFIG)
    merged.update(new_blob or {})
    payload = json.dumps(merged, sort_keys=True)
    now = utils.iso_now()
    with data_processor.connect() as conn:
        conn.execute("UPDATE trade_configs SET active = 0 WHERE active = 1")
        conn.execute(
            "INSERT INTO trade_configs (created_at, json_blob, active) VALUES (?, ?, 1)",
            (now, payload),
        )
        conn.commit()
    logger.info("trade.persistence: config saved")
    return merged


# ── Orders ─────────────────────────────────────────────────────────────


def insert_order(conn: sqlite3.Connection, fields: dict[str, Any]) -> int:
    """Insert an orders row. Returns the new row id. Caller must hold the
    transaction. UNIQUE(instrument, signal_timestamp, intent) and
    UNIQUE(client_order_ref) raise sqlite3.IntegrityError on duplicate —
    callers must handle this."""
    columns = [
        "client_order_ref", "broker_order_id",
        "instrument", "instrument_token", "strike", "option_type",
        "transaction_type", "qty", "lots", "price",
        "status", "intent", "parent_position_id", "mode",
        "signal_timestamp", "placed_at", "error",
    ]
    values = tuple(fields.get(c) for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    col_sql = ", ".join(columns)
    cur = conn.execute(
        f"INSERT INTO orders ({col_sql}) VALUES ({placeholders})",
        values,
    )
    return int(cur.lastrowid)


def orders_for_date(date: str) -> list[dict[str, Any]]:
    with data_processor.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM orders
            WHERE substr(placed_at, 1, 10) = ?
            ORDER BY placed_at DESC
            """,
            (date,),
        ).fetchall()
    return [dict(r) for r in rows]


def last_entry_time_for_instrument(instrument: str, date: str) -> str | None:
    with data_processor.connect() as conn:
        row = conn.execute(
            """
            SELECT MAX(placed_at) AS ts FROM orders
            WHERE instrument = ? AND intent = 'entry'
              AND substr(placed_at, 1, 10) = ?
            """,
            (instrument, date),
        ).fetchone()
    return row["ts"] if row and row["ts"] else None


def count_entries_today(date: str) -> int:
    with data_processor.connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM orders
            WHERE intent = 'entry' AND substr(placed_at, 1, 10) = ?
            """,
            (date,),
        ).fetchone()
    return int(row["n"] or 0)


# ── Positions ──────────────────────────────────────────────────────────


def insert_position(conn: sqlite3.Connection, fields: dict[str, Any]) -> int:
    columns = [
        "instrument", "instrument_token", "strike", "option_type",
        "qty", "lots", "entry_order_id", "entry_price", "entry_time",
        "status", "high_watermark", "sl_price", "target_price",
        "mode", "signal_timestamp",
        "ctx_oi_difference", "ctx_pcr", "ctx_ce_cumm", "ctx_pe_cumm", "ctx_margin",
    ]
    values = tuple(fields.get(c) for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    col_sql = ", ".join(columns)
    cur = conn.execute(
        f"INSERT INTO positions ({col_sql}) VALUES ({placeholders})",
        values,
    )
    return int(cur.lastrowid)


def open_positions() -> list[dict[str, Any]]:
    with data_processor.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY entry_time"
        ).fetchall()
    return [dict(r) for r in rows]


def has_open_for_instrument(instrument: str) -> bool:
    with data_processor.connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM positions
            WHERE instrument = ? AND status IN ('open', 'exiting')
            LIMIT 1
            """,
            (instrument,),
        ).fetchone()
    return row is not None


def positions_for_date(date: str, status: str | None = None) -> list[dict[str, Any]]:
    where = ["substr(entry_time, 1, 10) = ?"]
    params: list[Any] = [date]
    if status:
        where.append("status = ?")
        params.append(status)
    with data_processor.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM positions
            WHERE {' AND '.join(where)}
            ORDER BY entry_time DESC
            """,
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


def update_position_tsl(position_id: int, high_watermark: float, sl_price: float) -> None:
    with data_processor.connect() as conn:
        conn.execute(
            """
            UPDATE positions
            SET high_watermark = ?, sl_price = ?
            WHERE id = ? AND status = 'open'
            """,
            (high_watermark, sl_price, position_id),
        )
        conn.commit()


def request_manual_exit(position_id: int) -> bool:
    """Flag a position for manual exit on the engine's next tick.
    Returns True if a still-open position was flagged."""
    with data_processor.connect() as conn:
        cur = conn.execute(
            """
            UPDATE positions
            SET manual_exit_requested = 1
            WHERE id = ? AND status = 'open' AND manual_exit_requested = 0
            """,
            (position_id,),
        )
        conn.commit()
    return cur.rowcount > 0


# ── Atomic open / close transactions ───────────────────────────────────


def open_position_atomic(
    *,
    order_fields: dict[str, Any],
    position_fields: dict[str, Any],
) -> tuple[int, int] | None:
    """Insert the entry order and matching position in one transaction.

    Returns ``(order_id, position_id)`` on success. Returns ``None`` when
    the UNIQUE(instrument, signal_timestamp, intent) constraint blocks the
    duplicate — the engine logs and moves on.
    """
    client_ref = order_fields.setdefault("client_order_ref", uuid.uuid4().hex)
    with data_processor.connect() as conn:
        try:
            conn.execute("BEGIN")
            order_id = insert_order(conn, order_fields)
            position_fields["entry_order_id"] = order_id
            position_id = insert_position(conn, position_fields)
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            logger.warning(
                "open_position blocked by integrity constraint (likely duplicate signal): %s",
                exc,
            )
            return None
    logger.info(
        "Position opened: id=%s instrument=%s side=%s strike=%s entry_price=%s qty=%s ref=%s",
        position_id,
        position_fields.get("instrument"),
        position_fields.get("option_type"),
        position_fields.get("strike"),
        position_fields.get("entry_price"),
        position_fields.get("qty"),
        client_ref,
    )
    return order_id, position_id


def close_position_atomic(
    *,
    position_id: int,
    exit_order_fields: dict[str, Any],
    exit_price: float,
    exit_reason: str,
    entry_price: float,
    qty: int,
) -> int | None:
    """Mark the position exiting, insert the exit order, mark closed with pnl.

    Returns the exit order id on success, ``None`` if the position was
    already closed (race) or the order insert failed.
    """
    exit_order_fields.setdefault("client_order_ref", uuid.uuid4().hex)
    pnl = (exit_price - entry_price) * qty
    now = utils.iso_now()
    with data_processor.connect() as conn:
        try:
            conn.execute("BEGIN")
            cur = conn.execute(
                """
                UPDATE positions
                SET status = 'exiting'
                WHERE id = ? AND status = 'open'
                """,
                (position_id,),
            )
            if cur.rowcount == 0:
                conn.rollback()
                logger.info(
                    "close_position: position id=%s already not-open, skipping",
                    position_id,
                )
                return None
            exit_order_id = insert_order(conn, exit_order_fields)
            conn.execute(
                """
                UPDATE positions
                SET status = 'closed',
                    exit_order_id = ?,
                    exit_price = ?,
                    exit_time = ?,
                    exit_reason = ?,
                    pnl = ?,
                    manual_exit_requested = 0
                WHERE id = ?
                """,
                (exit_order_id, exit_price, now, exit_reason, pnl, position_id),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            logger.warning("close_position integrity error: %s", exc)
            return None
    logger.info(
        "Position closed: id=%s reason=%s exit_price=%s pnl=%.2f",
        position_id, exit_reason, exit_price, pnl,
    )
    return exit_order_id


# ── Audit log ──────────────────────────────────────────────────────────


def audit(
    kind: str,
    *,
    instrument: str | None = None,
    client_order_ref: str | None = None,
    position_id: int | None = None,
    gate: str | None = None,
    message: str | None = None,
) -> None:
    """Append an audit row. Never raises — audit failures must not break
    the engine."""
    try:
        with data_processor.connect() as conn:
            conn.execute(
                """
                INSERT INTO order_audit
                    (ts, kind, instrument, client_order_ref, position_id, gate, message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (utils.iso_now(), kind, instrument, client_order_ref, position_id, gate, message),
            )
            conn.commit()
    except Exception:
        logger.exception("audit log write failed")


def recent_audit(limit: int = 200) -> list[dict[str, Any]]:
    with data_processor.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM order_audit ORDER BY ts DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Daily trade reports ────────────────────────────────────────────────


def save_daily_report(date: str, report: dict[str, Any]) -> None:
    payload = json.dumps(report, default=str, sort_keys=True)
    with data_processor.connect() as conn:
        conn.execute(
            """
            INSERT INTO daily_trade_reports (date, generated_at, json_blob)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                generated_at = excluded.generated_at,
                json_blob = excluded.json_blob
            """,
            (date, utils.iso_now(), payload),
        )
        conn.commit()


def get_daily_report(date: str) -> dict[str, Any] | None:
    with data_processor.connect() as conn:
        row = conn.execute(
            "SELECT date, generated_at, json_blob FROM daily_trade_reports WHERE date = ?",
            (date,),
        ).fetchone()
    if not row:
        return None
    try:
        body = json.loads(row["json_blob"])
    except (ValueError, TypeError):
        body = {}
    body["date"] = row["date"]
    body["generated_at"] = row["generated_at"]
    return body


def list_report_dates() -> list[str]:
    """Every date for which we have either a snapshotted report OR positions."""
    with data_processor.connect() as conn:
        a = conn.execute("SELECT date FROM daily_trade_reports").fetchall()
        b = conn.execute(
            "SELECT DISTINCT substr(entry_time, 1, 10) AS d FROM positions"
        ).fetchall()
    dates = {r["date"] for r in a} | {r["d"] for r in b if r["d"]}
    return sorted(dates, reverse=True)
