"""All Upstox broker API calls used by the Index PCR backend."""

from __future__ import annotations

import logging
from typing import Any

import requests

import utilities as utils

logger = logging.getLogger(__name__)

_token_cache: str | None = None
_expiry_cache: dict[str, str] = {}


class BrokerAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _credential_token(creds: dict[str, Any]) -> str | None:
    for key in ("access_token", "analytics_token", "token"):
        value = creds.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _exchange_auth_code(creds: dict[str, Any]) -> str:
    auth_code = creds.get("auth_code") or creds.get("code")
    if not auth_code:
        raise BrokerAPIError("access_token missing and no auth_code/code present")

    response = requests.post(
        utils.upstox_token_url(),
        data={
            "code": auth_code,
            "client_id": creds["api_key"],
            "client_secret": creds["api_secret"],
            "redirect_uri": creds["redirect_uri"],
            "grant_type": "authorization_code",
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=utils.http_timeout(),
    )
    if response.status_code != 200:
        raise BrokerAPIError(
            f"token exchange failed: HTTP {response.status_code} {response.text[:500]}",
            response.status_code,
        )
    parsed = response.json()
    token = parsed.get("access_token") or (parsed.get("data") or {}).get("access_token")
    if not token:
        raise BrokerAPIError("token exchange response did not include access_token")
    return str(token)


def get_token() -> str:
    global _token_cache
    if _token_cache:
        return _token_cache

    creds = utils.upstox_config()
    token = _credential_token(creds)
    if token:
        _token_cache = token
        logger.info("Upstox token loaded from configs.json")
        return _token_cache

    _token_cache = _exchange_auth_code(creds)
    logger.info("Upstox token exchanged from auth code")
    return _token_cache


def invalidate_token() -> None:
    global _token_cache
    _token_cache = None
    logger.warning("Upstox token invalidated")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_token()}", "Accept": "application/json"}


def _get(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    retry_on_401: bool = False,
) -> dict[str, Any]:
    url = f"{utils.upstox_base_url()}/{path.lstrip('/')}"
    response = requests.get(url, headers=_headers(), params=params or {}, timeout=utils.http_timeout())
    if response.status_code == 401 and retry_on_401:
        invalidate_token()
        response = requests.get(url, headers=_headers(), params=params, timeout=utils.http_timeout())
    if response.status_code != 200:
        raise BrokerAPIError(
            f"GET {path} failed: HTTP {response.status_code} {response.text[:500]}",
            response.status_code,
        )
    parsed = response.json()
    if parsed.get("status") != "success":
        raise BrokerAPIError(f"GET {path} failed: {parsed}", response.status_code)
    return parsed


def get_exchange_status(exchange: str = "NSE") -> dict[str, Any]:
    parsed = _get(f"market/status/{exchange}", retry_on_401=True)
    data = parsed.get("data") or {}
    if not isinstance(data, dict):
        raise BrokerAPIError("market status response data is not an object")
    return data


def get_user_profile() -> dict[str, Any]:
    """Call Upstox /v2/user/profile using the configured access token."""
    parsed = _get("user/profile", retry_on_401=True)
    data = parsed.get("data") or {}
    if not isinstance(data, dict):
        raise BrokerAPIError("user profile response data is not an object")
    return data


def is_exchange_open(exchange_status: dict[str, Any]) -> bool:
    status = str(exchange_status.get("status") or "").upper()
    return status.endswith("_OPEN") or status == "OPEN"


def get_option_contracts(instrument_key: str) -> list[dict[str, Any]]:
    parsed = _get("option/contract", {"instrument_key": instrument_key})
    data = parsed.get("data") or []
    if not isinstance(data, list):
        raise BrokerAPIError("option contract response data is not a list")
    return data


def get_option_chain(instrument_key: str, expiry_date: str) -> list[dict[str, Any]]:
    parsed = _get(
        "option/chain",
        {"instrument_key": instrument_key, "expiry_date": expiry_date},
        retry_on_401=True,
    )
    data = parsed.get("data") or []
    if not isinstance(data, list):
        raise BrokerAPIError("option chain response data is not a list")
    return sorted(data, key=lambda row: row.get("strike_price") or 0)


def resolve_expiry(instrument: str, *, force: bool = False) -> str:
    name = utils.normalize_instrument_name(instrument)
    if not force and name in _expiry_cache:
        return _expiry_cache[name]
    cfg = utils.instrument_config(name)
    contracts = get_option_contracts(str(cfg["instrument_key"]))
    expiry = utils.choose_nearest_expiry(utils.extract_expiry_dates(contracts))
    _expiry_cache[name] = expiry
    logger.info("Resolved expiry for %s: %s", name, expiry)
    return expiry


def resolve_all_expiries(*, force: bool = False) -> dict[str, str]:
    return {name: resolve_expiry(name, force=force) for name in utils.instrument_names()}
