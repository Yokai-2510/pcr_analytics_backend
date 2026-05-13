"""Index PCR backend entry point."""

from __future__ import annotations

import logging
import sys

import broker_api
import data_processor
import market_data
import utilities as utils

logger = logging.getLogger(__name__)


def run_fetch_tick(expiries: dict[str, str]) -> dict[str, object]:
    raw_data = market_data.fetch_option_chains(expiries)
    fetch_summary = market_data.summarize_fetch(raw_data)
    persist_summary = data_processor.persist_market_data(raw_data)
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


def start_session(session_date: str) -> dict[str, str]:
    broker_api.get_token()
    expiries = broker_api.resolve_all_expiries(force=True)
    logger.info("Market collector session started for %s: %s", session_date, expiries)
    return expiries


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
        broker_api.get_token()
        expiries = broker_api.resolve_all_expiries(force=True)
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


def run_market_session() -> None:
    session_date: str | None = None
    expiries: dict[str, str] | None = None
    post_settlement_saved = False
    market_open_saved = False
    prev_close_saved = False

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
            utils.set_status(
                running=False,
                collector_running=False,
                market_state=configured_state,
                next_fetch=utils.next_collector_wakeup_iso(),
            )
            logger.info("Market worker skipped because today is not a trading weekday")
            return

        if configured_state == "closed":
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

                # ── Wait until precise market-open time (09:15:00) ────────
                # The systemd timer starts the worker at 09:10 but NSE opens
                # at 09:15.  Block here so the first tick is at 09:15:00
                # instead of 09:11 or 09:16.
                utils.wait_until_market_open()

            run_fetch_tick(expiries or {})
            utils.set_status(exchange_status=exchange_status)

            # ── Save market_open baseline on the very first tick ──────────
            if not market_open_saved:
                data_processor.save_baseline("market_open", date=session_date)
                market_open_saved = True
                logger.info(
                    "✅ market_open baseline recorded at %s for %s",
                    utils.iso_now(), session_date,
                )

            # ── Save post_settlement baseline after first tick ────────────
            if not post_settlement_saved:
                data_processor.save_baseline("post_settlement", date=session_date)
                post_settlement_saved = True

            utils.wait_until_next_fetch()
            continue

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
    run_market_session()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        utils.set_status(running=False, collector_running=False)
        logger.info("Interrupted; exiting")
        sys.exit(130)
    except Exception:
        utils.set_status(running=False, collector_running=False)
        logger.exception("Fatal backend error")
        raise
