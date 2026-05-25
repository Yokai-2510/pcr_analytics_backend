"""The 1-second execution loop for paper trading.

Two passes per tick:
  Pass 1 — exits: every open position is evaluated against the exit rules
           in priority order, first match wins.
  Pass 2 — entries: every enabled instrument is evaluated against the
           entry gates; on green light, a position is opened on the side
           the entry-router rule picks.

State lives entirely in SQLite. The engine has zero in-memory state
between ticks. Loop body is wrapped in a broad except so a single bad
tick can never kill the thread.
"""

from __future__ import annotations

from contextlib import closing
import logging
from datetime import datetime, time as dt_time
from typing import Any

import data_processor
import utilities as utils

from trade import broker as broker_mod
from trade import persistence, strikes

logger = logging.getLogger(__name__)


# ── Public entry point ────────────────────────────────────────────────


def tick() -> None:
    """Run one engine tick. Called by the thread loop every 1s.
    Catches any exception inside the work so the thread keeps ticking."""
    try:
        config = persistence.get_active_config()
        now = utils.now_ist()
        today = utils.today_ist()
        b = broker_mod.get_broker(config.get("mode", "paper"))
        _evaluate_exits(config, today, now, b)
        _evaluate_entries(config, today, now, b)
    except Exception as exc:  # noqa: BLE001
        logger.exception("engine tick failed")
        persistence.audit("engine_error", message=f"tick failed: {exc}")


# ── Pass 1: exits ─────────────────────────────────────────────────────


def _evaluate_exits(
    config: dict[str, Any],
    today: str,
    now: datetime,
    b: broker_mod.Broker,
) -> None:
    for position in persistence.open_positions():
        try:
            _evaluate_one_exit(position, config, today, now, b)
        except Exception as exc:  # noqa: BLE001
            logger.exception("exit evaluation failed for position %s", position.get("id"))
            persistence.audit(
                "engine_error",
                position_id=position.get("id"),
                instrument=position.get("instrument"),
                message=f"exit eval failed: {exc}",
            )


def _evaluate_one_exit(
    position: dict[str, Any],
    config: dict[str, Any],
    today: str,
    now: datetime,
    b: broker_mod.Broker,
) -> None:
    pid = int(position["id"])
    instrument = position["instrument"]
    side = position["option_type"]
    strike = float(position["strike"])
    entry_price = float(position["entry_price"])
    qty = int(position["qty"])

    ltp = strikes.latest_ltp(instrument, strike, side, today)
    if ltp is None:
        # Skip silently; this is normal pre-market or during data gaps.
        return

    # Priority 0: manual exit (user clicked the Close button)
    if int(position.get("manual_exit_requested") or 0) == 1:
        _close(position, "exit_manual", ltp, b)
        return

    # Priority 1: end-of-day force close
    market_close = _parse_hhmm(str(config.get("market_close_time") or "15:30"))
    eod_force = _at_or_after(now, _shift(market_close, seconds=-5))
    if eod_force:
        _close(position, "exit_eod", ltp, b)
        return

    # Priority 2: configured time-based exit
    if config.get("time_exit_enabled") and config.get("time_exit_at"):
        time_exit = _parse_hhmm(str(config["time_exit_at"]))
        if _at_or_after(now, time_exit):
            _close(position, "exit_time", ltp, b)
            return

    # Priority 3: counter-crossover (opposite-direction signal from data_engine)
    if config.get("exit_on_counter_crossover", True):
        opp = "SELL" if position_is_long_side(side) else "BUY"  # we always long; opp = SELL
        # Both BUY and SELL signals can exit a long; counter-crossover means SELL.
        # The data_engine emits BUY on entry crossover, SELL on exit crossover.
        latest = _latest_signal_after(
            instrument, today, after_iso=position["entry_time"],
        )
        if latest and latest.get("signal") == "SELL":
            _close(position, "exit_crossover", ltp, b)
            return

    # Priority 4: stop loss
    sl_price = position.get("sl_price")
    if sl_price is not None and ltp <= float(sl_price):
        _close(position, "exit_sl", ltp, b)
        return

    # Priority 5: target
    target_price = position.get("target_price")
    if target_price is not None and ltp >= float(target_price):
        _close(position, "exit_target", ltp, b)
        return

    # Priority 6: trailing SL update (no exit this tick, just state)
    if config.get("trailing_sl_enabled"):
        _maybe_ratchet_tsl(position, ltp, entry_price, config)


def position_is_long_side(side: str) -> bool:
    # Paper trading buys options outright — always long. Kept as a helper in
    # case we ever support short legs.
    return True


def _latest_signal_after(
    instrument: str,
    date: str,
    after_iso: str,
) -> dict[str, Any] | None:
    with closing(data_processor.connect()) as conn:
        row = conn.execute(
            """
            SELECT timestamp, signal, crossover
            FROM computed_ticks
            WHERE instrument = ?
              AND substr(timestamp, 1, 10) = ?
              AND timestamp > ?
              AND signal IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (instrument, date, after_iso),
        ).fetchone()
    return dict(row) if row else None


def _maybe_ratchet_tsl(
    position: dict[str, Any],
    ltp: float,
    entry_price: float,
    config: dict[str, Any],
) -> None:
    trigger_pct = float(config.get("trailing_sl_trigger_pct") or 0)
    step_pct = float(config.get("trailing_sl_step_pct") or 0)
    if trigger_pct <= 0 or step_pct <= 0:
        return
    hwm = position.get("high_watermark")
    if hwm is None or ltp > float(hwm):
        hwm = ltp
    armed = hwm >= entry_price * (1 + trigger_pct / 100.0)
    if not armed:
        # update HWM only
        if position.get("high_watermark") is None or hwm > float(position["high_watermark"]):
            persistence.update_position_tsl(
                int(position["id"]),
                high_watermark=hwm,
                sl_price=float(position.get("sl_price") or 0),
            )
        return
    new_sl = hwm * (1 - step_pct / 100.0)
    current_sl = float(position.get("sl_price") or 0)
    if new_sl > current_sl:
        persistence.update_position_tsl(int(position["id"]), high_watermark=hwm, sl_price=new_sl)
        persistence.audit(
            "tsl_ratchet",
            position_id=int(position["id"]),
            instrument=position.get("instrument"),
            message=f"hwm={hwm:.2f} sl={new_sl:.2f}",
        )


def _close(
    position: dict[str, Any],
    reason: str,
    ltp: float,
    b: broker_mod.Broker,
) -> None:
    result = b.place_exit(position=position, intent=reason, ref_price=ltp)
    if not result.success:
        persistence.audit(
            "engine_error",
            position_id=int(position["id"]),
            instrument=position.get("instrument"),
            message=f"broker exit failed: {result.error}",
        )
        return
    exit_order_id = persistence.close_position_atomic(
        position_id=int(position["id"]),
        exit_order_fields=dict(
            client_order_ref=result.client_order_ref,
            broker_order_id=result.broker_order_id,
            instrument=position["instrument"],
            instrument_token=position["instrument_token"],
            strike=position["strike"],
            option_type=position["option_type"],
            transaction_type="SELL",
            qty=position["qty"],
            lots=position["lots"],
            price=result.price,
            status="filled",
            intent=reason,
            parent_position_id=int(position["id"]),
            mode=position["mode"],
            signal_timestamp=None,
            placed_at=utils.iso_now(),
            error=None,
        ),
        exit_price=float(result.price or ltp),
        exit_reason=reason,
        entry_price=float(position["entry_price"]),
        qty=int(position["qty"]),
    )
    if exit_order_id is not None:
        persistence.audit(
            "exit_placed",
            position_id=int(position["id"]),
            instrument=position.get("instrument"),
            client_order_ref=result.client_order_ref,
            message=f"reason={reason} price={result.price}",
        )


# ── Pass 2: entries ───────────────────────────────────────────────────


def _evaluate_entries(
    config: dict[str, Any],
    today: str,
    now: datetime,
    b: broker_mod.Broker,
) -> None:
    if not config.get("auto_execute"):
        return
    instruments = list(config.get("instruments") or [])
    if not instruments:
        return
    # Cheap whole-config check: have we already hit the daily cap?
    if persistence.count_entries_today(today) >= int(config.get("max_positions_per_day") or 0 or 999):
        # Don't audit per-instrument when capped — would flood the log.
        return
    for instrument in instruments:
        try:
            _evaluate_one_entry(instrument, config, today, now, b)
        except Exception as exc:  # noqa: BLE001
            logger.exception("entry evaluation failed for %s", instrument)
            persistence.audit(
                "engine_error",
                instrument=instrument,
                message=f"entry eval failed: {exc}",
            )


def _evaluate_one_entry(
    instrument: str,
    config: dict[str, Any],
    today: str,
    now: datetime,
    b: broker_mod.Broker,
) -> None:
    # Gate: don't pyramid — one open position per instrument
    if persistence.has_open_for_instrument(instrument):
        return  # silent; we'd flood the audit log otherwise

    # Gate: cooldown
    cooldown_min = int(config.get("cooldown_minutes") or 0)
    if cooldown_min > 0:
        last_ts = persistence.last_entry_time_for_instrument(instrument, today)
        if last_ts and _iso_seconds_ago(last_ts, now) < cooldown_min * 60:
            return  # silent

    # Pull the latest BUY+crossover for this instrument today
    signal = _latest_buy_crossover(instrument, today)
    if not signal:
        return  # no fresh signal — silent

    # Gate: stale signal (engine running too far behind)
    stale_s = _iso_seconds_ago(signal["timestamp"], now)
    if stale_s > 90:
        return  # silent — would log every tick otherwise

    # Don't re-enter on a signal we've already acted on
    if _entry_already_exists(instrument, signal["timestamp"]):
        return  # silent

    # Decide side from the *previous* tick's cumulative OI
    prev = _previous_computed_tick(instrument, today, signal["timestamp"])
    if not prev:
        persistence.audit(
            "gate_reject", instrument=instrument, gate="NO_PREV_TICK",
            message=f"no tick before {signal['timestamp']}",
        )
        return
    prev_ce = utils.safe_float(prev.get("ce_oi_cumm_change"))
    prev_pe = utils.safe_float(prev.get("pe_oi_cumm_change"))
    current_diff = utils.safe_float(signal.get("oi_difference"))
    side = _decide_side(prev_ce, prev_pe, current_diff)
    if side is None:
        persistence.audit(
            "gate_reject", instrument=instrument, gate="TIE_OR_MISSING_CUMM",
            message=f"ce_cumm={prev_ce} pe_cumm={prev_pe} diff={current_diff}",
        )
        return

    # Resolve the leg to trade
    leg = strikes.resolve(
        instrument=instrument,
        side=side,
        strike_mode=str(config.get("strike_mode") or "atm"),
        custom_strike=config.get("custom_strike"),
        date=today,
    )
    if leg is None:
        persistence.audit(
            "gate_reject", instrument=instrument, gate="STRIKE_RESOLVE",
            message=f"could not resolve {side} for {instrument}",
        )
        return

    # Compute qty
    lot_size = _lot_size(instrument)
    lots = max(1, int(config.get("lots") or 1))
    qty = lot_size * lots

    # Place via broker
    result = b.place_entry(
        instrument=instrument,
        instrument_token=leg["token"],
        strike=leg["strike"],
        option_type=side,
        qty=qty,
        lots=lots,
        signal_timestamp=signal["timestamp"],
        ref_price=leg["ltp"],
    )
    if not result.success:
        persistence.audit(
            "engine_error", instrument=instrument,
            message=f"broker entry failed: {result.error}",
        )
        return

    fill_price = float(result.price or leg["ltp"])

    # Compute SL / target as absolute prices, frozen at entry
    sl_price = (
        round(fill_price * (1 - float(config["stop_loss_pct"]) / 100.0), 2)
        if config.get("stop_loss_enabled") and config.get("stop_loss_pct")
        else None
    )
    target_price = (
        round(fill_price * (1 + float(config["target_pct"]) / 100.0), 2)
        if config.get("target_enabled") and config.get("target_pct")
        else None
    )

    opened = persistence.open_position_atomic(
        order_fields=dict(
            client_order_ref=result.client_order_ref,
            broker_order_id=result.broker_order_id,
            instrument=instrument,
            instrument_token=leg["token"],
            strike=leg["strike"],
            option_type=side,
            transaction_type="BUY",
            qty=qty,
            lots=lots,
            price=fill_price,
            status="filled",
            intent="entry",
            parent_position_id=None,
            mode=b.mode,
            signal_timestamp=signal["timestamp"],
            placed_at=utils.iso_now(),
            error=None,
        ),
        position_fields=dict(
            instrument=instrument,
            instrument_token=leg["token"],
            strike=leg["strike"],
            option_type=side,
            qty=qty,
            lots=lots,
            entry_price=fill_price,
            entry_time=utils.iso_now(),
            status="open",
            high_watermark=fill_price,
            sl_price=sl_price,
            target_price=target_price,
            mode=b.mode,
            signal_timestamp=signal["timestamp"],
            ctx_oi_difference=utils.safe_float(signal.get("oi_difference")),
            ctx_pcr=utils.safe_float(signal.get("pcr")),
            ctx_ce_cumm=prev_ce,
            ctx_pe_cumm=prev_pe,
            ctx_margin=abs((prev_pe or 0) - (prev_ce or 0)),
        ),
    )
    if opened is None:
        # The UNIQUE constraint caught a duplicate — already audited downstream
        return
    persistence.audit(
        "entry_placed", instrument=instrument,
        client_order_ref=result.client_order_ref,
        message=f"side={side} strike={leg['strike']} price={fill_price} qty={qty}",
    )


def _latest_buy_crossover(instrument: str, date: str) -> dict[str, Any] | None:
    with closing(data_processor.connect()) as conn:
        row = conn.execute(
            """
            SELECT timestamp, signal, crossover, oi_difference, pcr,
                   ce_oi_cumm_change, pe_oi_cumm_change
            FROM computed_ticks
            WHERE instrument = ?
              AND substr(timestamp, 1, 10) = ?
              AND signal = 'BUY'
              AND crossover = 1
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (instrument, date),
        ).fetchone()
    return dict(row) if row else None


def _previous_computed_tick(
    instrument: str,
    date: str,
    timestamp: str,
) -> dict[str, Any] | None:
    with closing(data_processor.connect()) as conn:
        row = conn.execute(
            """
            SELECT timestamp, ce_oi_cumm_change, pe_oi_cumm_change
            FROM computed_ticks
            WHERE instrument = ?
              AND substr(timestamp, 1, 10) = ?
              AND timestamp < ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (instrument, date, timestamp),
        ).fetchone()
    return dict(row) if row else None


def _decide_side(
    prev_ce_cumm: float | None,
    prev_pe_cumm: float | None,
    current_diff: float | None,
) -> str | None:
    """Pick the leg to buy at a BUY+crossover tick.

    Rule (user's): the *Difference* (PE_cumm - CE_cumm) flipping its sign at
    this tick decides the side.
        - prev_diff <= 0 and current_diff > 0  (flip -ve to +ve)  ->  BUY CE
        - prev_diff >= 0 and current_diff < 0  (flip +ve to -ve)  ->  BUY PE

    Forced first BUY of the day (data_engine emits a forced BUY at the second
    tick of the session even without a real sign change) defaults to CE so the
    day opens with a long-bullish entry, matching the verified-good behaviour
    of session open.
    """
    prev_diff: float | None = None
    if prev_ce_cumm is not None and prev_pe_cumm is not None:
        prev_diff = float(prev_pe_cumm) - float(prev_ce_cumm)

    if prev_diff is not None and current_diff is not None:
        if prev_diff <= 0 and current_diff > 0:
            return "CE"
        if prev_diff >= 0 and current_diff < 0:
            return "PE"

    # No diff sign-change -- this is the forced first BUY of the day. Default
    # to CE (bullish open) per "first trade is alright" expectation.
    return "CE"


def _entry_already_exists(instrument: str, signal_timestamp: str) -> bool:
    with closing(data_processor.connect()) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM orders
            WHERE instrument = ?
              AND signal_timestamp = ?
              AND intent = 'entry'
            LIMIT 1
            """,
            (instrument, signal_timestamp),
        ).fetchone()
    return row is not None


def _lot_size(instrument: str) -> int:
    cfg = utils.instrument_config(instrument)
    # The backend config doesn't yet carry lot sizes; fall back to a sane default.
    explicit = cfg.get("lot_size")
    if explicit:
        return int(explicit)
    defaults = {"nifty": 75, "banknifty": 30, "sensex": 20}
    return defaults.get(instrument, 1)


# ── Time helpers ──────────────────────────────────────────────────────


def _parse_hhmm(value: str) -> dt_time:
    h, m = value.split(":")
    return dt_time(int(h), int(m), 0)


def _shift(t: dt_time, *, seconds: int) -> dt_time:
    total = t.hour * 3600 + t.minute * 60 + t.second + seconds
    total = max(0, min(86399, total))
    return dt_time(total // 3600, (total % 3600) // 60, total % 60)


def _at_or_after(now: datetime, t: dt_time) -> bool:
    return (now.hour, now.minute, now.second) >= (t.hour, t.minute, t.second)


def _iso_seconds_ago(iso_ts: str, now: datetime) -> float:
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (TypeError, ValueError):
        return 1e9
    if ts.tzinfo is None:
        # Treat as IST naïve — match the rest of the codebase
        ts = ts.replace(tzinfo=now.tzinfo)
    return (now - ts).total_seconds()
