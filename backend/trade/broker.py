"""Broker abstraction for the trade engine.

A Broker has exactly two responsibilities — placing an entry order and
placing an exit order — and a single source of truth for the price each
fills at. PaperBroker simulates fills against the latest oi_snapshots
LTP. LiveBroker will land here later wrapping upstox.orders; the engine
will not need to change.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import utilities as utils

from trade import persistence, strikes

logger = logging.getLogger(__name__)

OptionType = Literal["CE", "PE"]


@dataclass(frozen=True)
class OrderResult:
    """The outcome of a single broker.place_* call."""

    success: bool
    client_order_ref: str
    broker_order_id: str | None
    price: float | None
    error: str | None = None


class Broker(Protocol):
    """Two-method protocol the engine talks to. Live broker will implement
    the same shape."""

    mode: str

    def place_entry(
        self,
        *,
        instrument: str,
        instrument_token: str,
        strike: float,
        option_type: OptionType,
        qty: int,
        lots: int,
        signal_timestamp: str,
        ref_price: float,
    ) -> OrderResult: ...

    def place_exit(
        self,
        *,
        position: dict[str, Any],
        intent: str,
        ref_price: float,
    ) -> OrderResult: ...


# ── Paper broker ───────────────────────────────────────────────────────


class PaperBroker:
    """Atomic fill at the supplied reference LTP. The engine reads LTP
    from the latest oi_snapshots row before calling us; we trust it.
    No external IO."""

    mode = "paper"

    def place_entry(
        self,
        *,
        instrument: str,
        instrument_token: str,
        strike: float,
        option_type: OptionType,
        qty: int,
        lots: int,
        signal_timestamp: str,
        ref_price: float,
    ) -> OrderResult:
        if ref_price is None or ref_price <= 0:
            return OrderResult(
                success=False, client_order_ref="", broker_order_id=None,
                price=None, error="invalid ref_price",
            )
        ref = uuid.uuid4().hex
        logger.info(
            "PaperBroker.place_entry: %s %s @ %s qty=%s ref=%s",
            instrument, option_type, ref_price, qty, ref,
        )
        return OrderResult(
            success=True, client_order_ref=ref, broker_order_id=None,
            price=float(ref_price), error=None,
        )

    def place_exit(
        self,
        *,
        position: dict[str, Any],
        intent: str,
        ref_price: float,
    ) -> OrderResult:
        if ref_price is None or ref_price <= 0:
            return OrderResult(
                success=False, client_order_ref="", broker_order_id=None,
                price=None, error="invalid ref_price",
            )
        ref = uuid.uuid4().hex
        logger.info(
            "PaperBroker.place_exit: pos=%s intent=%s @ %s qty=%s ref=%s",
            position.get("id"), intent, ref_price, position.get("qty"), ref,
        )
        return OrderResult(
            success=True, client_order_ref=ref, broker_order_id=None,
            price=float(ref_price), error=None,
        )


# ── Dispatch ───────────────────────────────────────────────────────────


_PAPER = PaperBroker()


def get_broker(mode: str) -> Broker:
    """Return the broker for the requested mode. Live mode is not yet
    implemented — falls back to paper with a warning so the engine can't
    silently leak real orders before LiveBroker is reviewed."""
    if mode == "live":
        logger.warning("live mode requested but LiveBroker not implemented; using PaperBroker")
        persistence.audit(
            "engine_error",
            message="live mode requested but LiveBroker not implemented; using PaperBroker",
        )
    return _PAPER
