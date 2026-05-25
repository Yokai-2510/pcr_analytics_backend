"""FastAPI router for the trade subsystem.

Mounted by ``backend/api.py``. Reads are public against the admin token
gate inherited from the main app; writes are admin-only via ``require_admin``.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

import utilities as utils

from trade import persistence, reports, strikes

router = APIRouter(prefix="/api/trade", tags=["trade"])


# Local copy of the admin dependency. Defined here rather than imported
# from backend/api.py to avoid the circular import (api.py mounts this
# router at module-import time).
def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    expected = utils.admin_api_key()
    if not expected:
        raise HTTPException(status_code=503, detail="admin API key is not configured")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="admin token required")


# ── Config ─────────────────────────────────────────────────────────────


class TradeConfigBody(BaseModel):
    config: dict[str, Any]


@router.get("/config")
def get_config() -> dict[str, Any]:
    return {"config": persistence.get_active_config()}


@router.put("/config", dependencies=[Depends(require_admin)])
def put_config(body: TradeConfigBody) -> dict[str, Any]:
    saved = persistence.save_config(body.config or {})
    return {"config": saved}


# ── Positions / orders ─────────────────────────────────────────────────


@router.get("/positions")
def list_positions(
    status: str | None = Query(default=None),
    date: str = Query(default_factory=utils.today_ist),
) -> dict[str, Any]:
    rows = persistence.positions_for_date(date, status=status)
    today = utils.today_ist()
    if date == today:
        for row in rows:
            if row.get("status") in ("open", "exiting"):
                ltp = strikes.latest_ltp(
                    row["instrument"], float(row["strike"]),
                    row["option_type"], date,
                )
                row["live_ltp"] = ltp
                if ltp is not None:
                    row["unrealized_pnl"] = round(
                        (ltp - float(row["entry_price"])) * int(row["qty"]), 2
                    )
                else:
                    row["unrealized_pnl"] = None
    return {"date": date, "count": len(rows), "positions": rows}


@router.get("/orders")
def list_orders(date: str = Query(default_factory=utils.today_ist)) -> dict[str, Any]:
    rows = persistence.orders_for_date(date)
    return {"date": date, "count": len(rows), "orders": rows}


@router.get("/summary")
def get_summary(date: str = Query(default_factory=utils.today_ist)) -> dict[str, Any]:
    return reports.build_report(date)


# ── Manual exit ────────────────────────────────────────────────────────


@router.post(
    "/positions/{position_id}/exit",
    dependencies=[Depends(require_admin)],
)
def request_exit(position_id: int) -> dict[str, Any]:
    ok = persistence.request_manual_exit(position_id)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"position {position_id} not open or already flagged",
        )
    persistence.audit(
        "exit_placed", position_id=position_id,
        message="manual exit requested via API",
    )
    return {"queued": True, "position_id": position_id}


# ── Daily reports ──────────────────────────────────────────────────────


@router.get("/reports")
def list_reports() -> dict[str, Any]:
    return {"dates": reports.list_dates()}


@router.get("/reports/{date}")
def get_report(date: str) -> dict[str, Any]:
    return reports.get_or_build(date)


@router.post(
    "/reports/{date}/snapshot",
    dependencies=[Depends(require_admin)],
)
def snapshot_report(date: str) -> dict[str, Any]:
    return reports.snapshot_date(date)


# ── Audit (debug) ──────────────────────────────────────────────────────


@router.get("/audit", dependencies=[Depends(require_admin)])
def get_audit(limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
    rows = persistence.recent_audit(limit=limit)
    return {"count": len(rows), "events": rows}
