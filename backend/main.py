"""Index PCR backend entry point."""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from datetime import timedelta

import broker_api
import data_engine
import data_processor
import market_data
import utilities as utils

logger = logging.getLogger(__name__)

# ── Compute tick interval (seconds) ─────────────────────────────────────
# FOR TESTING: set to 2 seconds. Production should be 60.
COMPUTE_TICK_INTERVAL = 2  # seconds between computed ticks (testing: 2s, prod: 60s)
RAW_FETCH_INTERVAL = 1     # seconds between raw fetches (testing: 1s, prod: 30s)

# Pre-open cache — resolved once before market open, consumed on first tick.
_preopen_expiries: dict[str, str] | None = None

# ── Compute tick thread ──────────────────────────────────────────────────
_compute_thread: threading.Thread | None = None
_compute_stop_event = threading.Event()


def _compute_tick_loop() -> None:
    """Background thread that fires compute at exact interval boundaries.

    Uses math.ceil(now / interval) * interval to align to exact boundaries.
    For testing (2s): fires at even-second boundaries.
    For production (60s): fires at exact minute boundaries (09:15:00, 09:16:00, etc.)
    """
    logger.info("Compute tick thread started (interval=%ds)", COMPUTE_TICK_INTERVAL)
    while not _compute_stop_event.is_set():
        try:
            # Calculate next boundary
            now_epoch = time.time()
            next_boundary = math.ceil(now_epoch / COMPUTE_TICK_INTERVAL) * COMPUTE_TICK_INTERVAL
            wait_time = next_boundary - time.time()

            if wait_time > 0:
                # Use event.wait() so we can be interrupted for shutdown
                if _compute_stop_event.wait(timeout=wait_time):
                    break  # Stop event was set

            # Fire compute cycle
            if not _compute_stop_event.is_set():
                today = utils.today_ist()
                results = data_engine.run_compute_cycle(today)
                logger.debug("Compute tick fired at boundary: %s", results)

        except Exception:
            logger.exception("Error in compute tick loop")
            # Brief sleep to avoid tight error loop
            if _compute_stop_event.wait(timeout=1.0):
                break

    logger.info("Compute tick thread stopped")


def start_compute_thread() -> None:
    """Start the background compute tick thread."""
    global _compute_thread
    if _compute_thread and _compute_thread.is_alive():
        return
    _compute_stop_event.clear()
    _compute_thread = threading.Thread(
        target=_compute_tick_loop,
        name="compute-tick",
        daemon=True,
    )
    _compute_thread.start()
    logger.info("Compute tick thread launched")


def stop_compute_thread() -> None:
    """Stop the background compute tick thread."""
    global _compute_thread
    _compute_stop_event.set()
    if _compute_thread and _compute_thread.is_alive():
        _compute_thread.join(timeout=5.0)
    _compute_thread = None
    logger.info("Compute tick thread joined")


def run_fetch_tick(expiries: dict[str, str], *, persist: bool = True) -> dict[str, object]:
    raw_data = market_data.fetch_option_chains(expiries)
    fetch_summary = market_data.summarize_fetch(raw_data)
    if persist:
        persist_summary = data_processor.persist_market_data(raw_data)
    else:
        persist_summary = {"skipped": True}
    utils.set_status(
        running=True,
        collector_running=True,
        market_state=utils.market_session_state(),
        last_fetch=utils.iso_now(),
        next_fetch=utils.next_collector_wakeup_iso(),
        last_error=", ".join(fetch_summary["failed"]) if fetch_summary["failed"] else None,
    )
    logger.info("Fetch tick complete: %s %s", fetch_summary, persist_summary)
    return {"fetch": fetch_summary, "persist": persist_summary}


def _prepare_session(date: str) -> dict[str, str]:
    """Resolve token + expiries.  Called during pre-open or on first tick."""
    broker_api.get_token()
    expiries = broker_api.resolve_all_expiries(force=True)
    logger.info("Session prepared for %s: %s", date, expiries)
    return expiries


def start_session(session_date: str) -> dict[str, str]:
    """Start a market session, using pre-resolved expiries if available."""
    global _preopen_expiries
    if _preopen_expiries is not None:
        expiries = _preopen_expiries
        _preopen_expiries = None
        logger.info("Market collector session started for %s (pre-resolved expiries): %s",
                     session_date, expiries)
        return expiries
    # Fallback: resolve now (shouldn't happen in normal flow)
    return _prepare_session(session_date)


def exchange_market_state() -> tuple[str, dict[str, object] | None]:
    status = broker_api.get_exchange_status("NSE")
    if broker_api.is_exchange_open(status):
        return "live", status
    return str(status.get("status") or "exchange_closed").lower(), status


def _save_prev_close_baseline() -> None:
    """Fetch option chains *before* market open and record prev_close baseline.

    The broker API returns previous-day option-chain data while the market is
    still closed.  We use that to freeze the previous trading day's closing OI
    so intraday OI-change charts have a proper reference point.
    """
    today = utils.today_ist()
    existing = data_processor.count_baseline_rows("prev_close", today)
    if any(count > 0 for count in existing.values()):
        logger.info("prev_close baseline already recorded for %s; skipping", today)
        return

    try:
        expiries = _prepare_session(today)
    except Exception:
        logger.exception("Failed to resolve expiries for prev_close baseline")
        return

    raw_data = market_data.fetch_option_chains(expiries)
    any_data = any(v is not None for v in raw_data.values())
    if not any_data:
        logger.warning("No option chain data available for prev_close baseline")
        return

    data_processor.persist_market_data(raw_data)
    counts = data_processor.save_baseline("prev_close", date=today)
    logger.info(
        "✅ prev_close baseline recorded at %s for %s: %s",
        utils.iso_now(), today, counts,
    )


def _preopen_prepare(today: str) -> None:
    """Pre-resolve expiries while waiting for market open.

    This ensures the first fetch tick happens at exactly 09:15:00 instead of
    09:16:00 (because resolving 3 instrument expiries takes ~60 seconds).
    """
    global _preopen_expiries
    if _preopen_expiries is not None:
        return  # Already resolved
    try:
        _preopen_expiries = _prepare_session(today)
        logger.info("✅ Pre-open expiries resolved at %s", utils.iso_now())
    except Exception:
        logger.exception("Pre-open expiry resolution failed; will retry at market open")


def _post_settlement_ready() -> bool:
    """Return True if we're at least 5 minutes past market open.

    The post_settlement baseline should capture OI *after* the initial
    post-open churn has settled, not the very first tick.  We use the
    configured ``market_open_time`` (default 09:15) and wait until
    ``settlement_delay_minutes`` past that.
    """
    cfg = utils.app_config()
    settlement_delay = int(cfg.get("settlement_delay_minutes", 5))
    open_t = utils.parse_hhmm(str(cfg.get("market_open_time") or cfg["market_start_time"]))
    now = utils.now_ist()
    open_dt = now.replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
    return now >= open_dt + timedelta(minutes=settlement_delay)


def run_market_session() -> None:
    session_date: str | None = None
    expiries: dict[str, str] | None = None
    post_settlement_saved = False
    market_open_saved = False
    prev_close_saved = False
    last_persist_epoch: float = 0.0

    # ── Phase 0: Record prev_close baseline (08:55 daily restart) ────────
    # The API is already running; the broker returns *yesterday's* option
    # chain while the market is closed.  Capture it now so the rest of the
    # day has a proper previous-close reference.
    if not prev_close_saved:
        _save_prev_close_baseline()
        prev_close_saved = True

    while True:
        configured_state = utils.market_session_state()
        today = utils.today_ist()

        if configured_state == "closed_weekend":
            stop_compute_thread()
            utils.set_status(
                running=False,
                collector_running=False,
                market_state=configured_state,
                next_fetch=utils.next_collector_wakeup_iso(),
            )
            logger.info("Market worker skipped because today is not a trading weekday")
            return

        if configured_state == "closed":
            stop_compute_thread()
            if session_date == today and post_settlement_saved and not prev_close_saved:
                data_processor.save_baseline("prev_close", date=session_date)
                prev_close_saved = True
                logger.info("Market collector session closed for %s", session_date)
            utils.set_status(
                running=False,
                collector_running=False,
                market_state=configured_state,
                next_fetch=utils.next_collector_wakeup_iso(),
            )
            logger.info("Market worker stopped after configured close")
            return

        if configured_state != "live":
            # ── Pre-open: resolve expiries early so first tick is at 09:15 ──
            _preopen_prepare(today)
            utils.set_status(
                running=True,
                collector_running=False,
                market_state=configured_state,
                next_fetch=utils.next_collector_wakeup_iso(),
            )
            utils.wait_while_idle()
            continue

        # ── Market is live ────────────────────────────────────────────────
        try:
            state, exchange_status = exchange_market_state()
        except broker_api.BrokerAPIError as exc:
            logger.exception("Unable to read NSE market status")
            utils.set_status(
                running=True,
                collector_running=False,
                market_state="exchange_status_error",
                last_error=str(exc),
                next_fetch=utils.next_collector_wakeup_iso(),
            )
            utils.wait_while_idle()
            continue

        if state == "live":
            if session_date != today:
                session_date = today
                expiries = start_session(session_date)
                post_settlement_saved = False
                market_open_saved = False
                last_persist_epoch = 0.0

                # ── Wait until precise market-open time (09:15:00) ────────
                utils.wait_until_market_open()

            # ── Start compute thread if not already running ───────────────
            start_compute_thread()

            # Fetch every RAW_FETCH_INTERVAL seconds (testing: 1s, prod: 30s)
            # Always persist raw data on every fetch for the new architecture
            run_fetch_tick(expiries or {}, persist=True)
            utils.set_status(exchange_status=exchange_status)

            # ── Save market_open baseline on the very first tick ──────────
            if not market_open_saved:
                data_processor.save_baseline("market_open", date=session_date)
                market_open_saved = True
                logger.info(
                    "market_open baseline recorded at %s for %s",
                    utils.iso_now(), session_date,
                )

            # ── Save post_settlement baseline after settlement delay ─────
            if not post_settlement_saved and _post_settlement_ready():
                data_processor.save_baseline("post_settlement", date=session_date)
                post_settlement_saved = True
                logger.info(
                    "post_settlement baseline recorded at %s for %s",
                    utils.iso_now(), session_date,
                )

            # Sleep for RAW_FETCH_INTERVAL (testing: 1s)
            time.sleep(RAW_FETCH_INTERVAL)
            continue

        # ── Exchange not live (pre-open, auction, etc.) ──────────────────
        stop_compute_thread()
        utils.set_status(
            running=True,
            collector_running=False,
            market_state=state,
            exchange_status=exchange_status,
            next_fetch=utils.next_collector_wakeup_iso(),
        )
        utils.wait_while_idle()


def main() -> None:
    utils.ensure_backend_config()
    utils.configure_logging()
    utils.set_status(running=True, collector_running=False)
    data_processor.initialize_storage()
    logger.info(
        "Worker starting with RAW_FETCH_INTERVAL=%ds, COMPUTE_TICK_INTERVAL=%ds",
        RAW_FETCH_INTERVAL, COMPUTE_TICK_INTERVAL,
    )
    run_market_session()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        stop_compute_thread()
        utils.set_status(running=False, collector_running=False)
        logger.info("Interrupted; exiting")
        sys.exit(130)
    except Exception:
        stop_compute_thread()
        utils.set_status(running=False, collector_running=False)
        logger.exception("Fatal backend error")
        raise
