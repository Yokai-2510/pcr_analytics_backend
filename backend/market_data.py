"""Market data fetch cycle helpers."""

from __future__ import annotations

import logging
from typing import Any

import requests

import broker_api
import utilities as utils

logger = logging.getLogger(__name__)


def prepare_fetch_plan(expiries: dict[str, str]) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for instrument, cfg in utils.instruments_config().items():
        expiry = expiries.get(instrument)
        if not expiry:
            continue
        plan.append(
            {
                "instrument": instrument,
                "instrument_key": cfg["instrument_key"],
                "expiry": expiry,
                "strike_step": cfg["strike_step"],
                "strike_count": cfg["strike_count"],
            }
        )
    return plan


def fetch_option_chains(expiries: dict[str, str]) -> dict[str, dict[str, Any] | None]:
    raw: dict[str, dict[str, Any] | None] = {}
    for item in prepare_fetch_plan(expiries):
        instrument = str(item["instrument"])
        try:
            strikes = broker_api.get_option_chain(
                str(item["instrument_key"]),
                str(item["expiry"]),
            )
            raw[instrument] = {
                "timestamp": utils.iso_now(),
                "instrument": instrument,
                "instrument_key": item["instrument_key"],
                "expiry": item["expiry"],
                "strikes": strikes,
            }
        except (broker_api.BrokerAPIError, requests.RequestException) as exc:
            logger.error("Option-chain fetch failed for %s: %s", instrument, exc)
            raw[instrument] = None
        except Exception:
            logger.exception("Unexpected option-chain failure for %s", instrument)
            raw[instrument] = None

    missing = set(utils.instrument_names()) - set(raw)
    for instrument in missing:
        logger.error("No fetch plan for %s; missing expiry", instrument)
        raw[instrument] = None
    return raw


def summarize_fetch(raw_data: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    return {
        "fetched": [name for name, payload in raw_data.items() if payload],
        "failed": [name for name, payload in raw_data.items() if not payload],
        "strike_counts": {
            name: len(payload.get("strikes") or []) if payload else 0
            for name, payload in raw_data.items()
        },
    }

