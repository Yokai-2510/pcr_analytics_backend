"""Trade subsystem entry point.

Public surface for the worker process:
  start_execution_thread()  — starts the 1s engine loop in a daemon thread.
  stop_execution_thread()   — signals the loop to stop, joins it, and snapshots
                              today's daily trade report.

The API process should NOT call ``start_execution_thread``. Only the
worker (``main.py``) owns the engine.
"""

from __future__ import annotations

import logging
import threading
import time

from trade import engine, reports
from trade.persistence import init_schema  # re-exported for data_processor

logger = logging.getLogger(__name__)

_ENGINE_TICK_INTERVAL = 1.0  # seconds between engine ticks

_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _loop() -> None:
    logger.info("trade engine thread started (interval=%ss)", _ENGINE_TICK_INTERVAL)
    # Align ticks to whole-second boundaries to avoid latency drift across
    # the day, the same pattern the fetch loop uses.
    while not _stop_event.is_set():
        next_boundary = (int(time.time()) // 1) * 1 + _ENGINE_TICK_INTERVAL
        sleep_for = max(0.0, next_boundary - time.time())
        if _stop_event.wait(timeout=sleep_for):
            break
        if _stop_event.is_set():
            break
        engine.tick()
    logger.info("trade engine thread stopped")


def start_execution_thread() -> None:
    """Start the engine thread. Idempotent."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="trade-engine", daemon=True)
    _thread.start()
    logger.info("trade engine thread launched")


def stop_execution_thread() -> None:
    """Signal the engine to stop, join it, then snapshot today's report."""
    global _thread
    _stop_event.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=5.0)
    _thread = None
    try:
        reports.snapshot_today()
    except Exception:
        logger.exception("snapshot_today failed on shutdown")
    logger.info("trade engine thread joined")


__all__ = [
    "start_execution_thread",
    "stop_execution_thread",
    "init_schema",
]
