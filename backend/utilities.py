"""Shared helpers and JSON-backed runtime configuration."""

from __future__ import annotations

import json
import logging
import math
import secrets
import time
from copy import deepcopy
from datetime import datetime, timedelta, time as dt_time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
SOURCE_DIR = PROJECT_DIR / "source"
CONFIGS_PATH = SOURCE_DIR / "configs.json"
CREDENTIALS_PATH = SOURCE_DIR / "credentials.json"
USER_PREFS_PATH = SOURCE_DIR / "user_prefs.json"

DEFAULT_USER_PREFS: dict[str, Any] = {
    "default_instrument": "nifty",
    "default_chart_ids": [],
    "charts_layout": "2x2",
    "charts_mode": "tabs",
    "last_page": "dashboard",
    "weekdays_enabled": ["Mon", "Tue", "Wed", "Thu", "Fri"],
}

DEFAULT_BACKEND_CONFIG: dict[str, Any] = {
    "timezone": "Asia/Kolkata",
    "fetch_interval_seconds": 60,
    "market_start_time": "09:10",
    "market_close_time": "15:30",
    "upstox_base_url": "https://api.upstox.com/v2",
    "upstox_token_url": "https://api.upstox.com/v2/login/authorization/token",
    "api": {"host": "0.0.0.0", "port": 8000},
    "schedule": {
        "api_mode": "always",
        "weekdays": "Mon..Fri",
        "daily_restart_time": "08:55",
        "worker_start_time": "09:10",
        "worker_stop_time": "15:30",
    },
    "paths": {"db": "data/oi_data.db", "logs": "logs", "status": "data/status.json"},
    "http": {"timeout_seconds": 15},
    "instruments": {
        "nifty": {
            "name": "Nifty",
            "instrument_key": "NSE_INDEX|Nifty 50",
            "strike_step": 50,
            "strike_count": 5,
        },
        "banknifty": {
            "name": "BankNifty",
            "instrument_key": "NSE_INDEX|Nifty Bank",
            "strike_step": 100,
            "strike_count": 5,
        },
        "sensex": {
            "name": "Sensex",
            "instrument_key": "BSE_INDEX|SENSEX",
            "strike_step": 100,
            "strike_count": 5,
        },
    },
}

STATUS: dict[str, Any] = {
    "running": False,
    "api_running": False,
    "collector_running": False,
    "market_state": "starting",
    "last_fetch": None,
    "next_fetch": None,
    "last_error": None,
}

_config_cache: dict[str, Any] | None = None
_credentials_cache: dict[str, Any] | None = None


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        parsed = json.load(f)
    if not isinstance(parsed, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return parsed


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    tmp.replace(path)
    if path == CREDENTIALS_PATH:
        try:
            path.chmod(0o600)
        except OSError:
            logging.getLogger(__name__).debug("Could not chmod credentials file")


def new_secret_token() -> str:
    return secrets.token_urlsafe(32)


def load_config(*, reload: bool = False) -> dict[str, Any]:
    global _config_cache
    if _config_cache is None or reload:
        if not CONFIGS_PATH.exists():
            raise FileNotFoundError(f"configs.json not found: {CONFIGS_PATH}")
        parsed = read_json(CONFIGS_PATH)
        parsed.setdefault("backend", deepcopy(DEFAULT_BACKEND_CONFIG))
        _config_cache = parsed
    return _config_cache


def save_config(data: dict[str, Any]) -> None:
    global _config_cache
    write_json(CONFIGS_PATH, data)
    _config_cache = data


def load_credentials(*, reload: bool = False) -> dict[str, Any]:
    global _credentials_cache
    if _credentials_cache is None or reload:
        if CREDENTIALS_PATH.exists():
            _credentials_cache = read_json(CREDENTIALS_PATH)
        else:
            _credentials_cache = {}
    return _credentials_cache


def save_credentials(data: dict[str, Any]) -> None:
    global _credentials_cache
    write_json(CREDENTIALS_PATH, data)
    _credentials_cache = data


def ensure_credentials() -> dict[str, Any]:
    creds = load_credentials()
    cfg = load_config()
    changed = False
    config_changed = False

    if "upstox" in cfg:
        upstox_from_config = cfg.pop("upstox")
        config_changed = True
        if isinstance(upstox_from_config, dict):
            upstox = creds.setdefault("upstox", {})
            for key, value in upstox_from_config.items():
                if key not in upstox:
                    upstox[key] = value
                    changed = True

    admin = creds.setdefault("admin", {})
    if not admin.get("api_key"):
        admin["api_key"] = new_secret_token()
        changed = True

    creds.setdefault("upstox", {})

    if changed or not CREDENTIALS_PATH.exists():
        save_credentials(creds)
    if config_changed:
        save_config(cfg)
    return creds


def ensure_backend_config() -> dict[str, Any]:
    cfg = load_config()
    backend = cfg.setdefault("backend", {})
    changed = False
    for key, value in DEFAULT_BACKEND_CONFIG.items():
        if key not in backend:
            backend[key] = deepcopy(value)
            changed = True
        elif isinstance(value, dict) and isinstance(backend.get(key), dict):
            for nested_key, nested_value in value.items():
                if nested_key not in backend[key]:
                    backend[key][nested_key] = deepcopy(nested_value)
                    changed = True
    for name, defaults in DEFAULT_BACKEND_CONFIG["instruments"].items():
        instruments = backend.setdefault("instruments", {})
        instrument = instruments.setdefault(name, {})
        for key, value in defaults.items():
            if key not in instrument:
                instrument[key] = deepcopy(value)
                changed = True
    if changed:
        save_config(cfg)
    return backend


def app_config() -> dict[str, Any]:
    return ensure_backend_config()


def upstox_config() -> dict[str, Any]:
    section = ensure_credentials().get("upstox") or {}
    if not isinstance(section, dict):
        raise ValueError("credentials.json upstox section must be an object")
    return section


def admin_api_key() -> str:
    admin = ensure_credentials().get("admin") or {}
    return str(admin.get("api_key") or "")


def instruments_config() -> dict[str, dict[str, Any]]:
    instruments = app_config()["instruments"]
    return instruments


def instrument_names() -> list[str]:
    return list(instruments_config().keys())


def normalize_instrument_name(instrument: str) -> str:
    name = instrument.lower()
    if name not in instruments_config():
        raise KeyError(f"unknown instrument: {instrument}")
    return name


def instrument_config(instrument: str) -> dict[str, Any]:
    return instruments_config()[normalize_instrument_name(instrument)]


def public_config() -> dict[str, Any]:
    cfg = load_config()
    cfg.pop("upstox", None)
    return deepcopy(app_config())


def schedule_config() -> dict[str, Any]:
    return app_config()["schedule"]


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _merge_allowed(target: dict[str, Any], updates: dict[str, Any], allowed: set[str]) -> None:
    for key, value in updates.items():
        if key not in allowed:
            raise ValueError(f"unsupported config field: {key}")
        target[key] = value


def validate_time_fields(updates: dict[str, Any], fields: set[str]) -> None:
    for field in fields:
        if field in updates and updates[field] is not None:
            parse_hhmm(str(updates[field]))


def update_backend_config(updates: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config()
    backend = cfg.setdefault("backend", deepcopy(DEFAULT_BACKEND_CONFIG))

    allowed_sections = {
        "timezone",
        "fetch_interval_seconds",
        "market_start_time",
        "market_close_time",
        "upstox_base_url",
        "upstox_token_url",
        "api",
        "http",
        "schedule",
        "instruments",
    }
    unsupported = set(updates) - allowed_sections
    if unsupported:
        raise ValueError(f"unsupported config field(s): {', '.join(sorted(unsupported))}")

    allowed_top = {
        "timezone",
        "fetch_interval_seconds",
        "market_start_time",
        "market_close_time",
        "upstox_base_url",
        "upstox_token_url",
    }
    top_updates = {key: value for key, value in updates.items() if key in allowed_top}
    validate_time_fields(top_updates, {"market_start_time", "market_close_time"})
    _merge_allowed(backend, top_updates, allowed_top)

    if "api" in updates:
        api = backend.setdefault("api", {})
        _merge_allowed(api, _require_object(updates["api"], "api"), {"host", "port"})

    if "http" in updates:
        http = backend.setdefault("http", {})
        _merge_allowed(http, _require_object(updates["http"], "http"), {"timeout_seconds"})

    if "schedule" in updates:
        schedule_updates = _require_object(updates["schedule"], "schedule")
        validate_time_fields(
            schedule_updates,
            {"daily_restart_time", "worker_start_time", "worker_stop_time"},
        )
        schedule = backend.setdefault("schedule", {})
        _merge_allowed(
            schedule,
            schedule_updates,
            {"api_mode", "weekdays", "daily_restart_time", "worker_start_time", "worker_stop_time"},
        )

    if "instruments" in updates:
        instruments_updates = _require_object(updates["instruments"], "instruments")
        instruments = backend.setdefault("instruments", {})
        for name, instrument_updates in instruments_updates.items():
            key = normalize_instrument_name(str(name))
            instrument = instruments.setdefault(key, {})
            _merge_allowed(
                instrument,
                _require_object(instrument_updates, f"instruments.{key}"),
                {"name", "instrument_key", "strike_step", "strike_count"},
            )

    schedule = backend.setdefault("schedule", {})
    if "market_start_time" in top_updates:
        schedule["worker_start_time"] = backend["market_start_time"]
    if "market_close_time" in top_updates:
        schedule["worker_stop_time"] = backend["market_close_time"]
    if "worker_start_time" in schedule:
        backend["market_start_time"] = schedule["worker_start_time"]
    if "worker_stop_time" in schedule:
        backend["market_close_time"] = schedule["worker_stop_time"]

    save_config(cfg)
    logging.getLogger(__name__).info("Backend config updated")
    return public_config()


def mask_secret(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {"configured": False, "preview": ""}
    text = str(value)
    if len(text) <= 8:
        preview = "*" * len(text)
    else:
        preview = f"{text[:3]}...{text[-3:]}"
    return {"configured": True, "preview": preview}


def public_credentials() -> dict[str, Any]:
    creds = ensure_credentials()
    return {
        section: {key: mask_secret(value) for key, value in values.items()}
        for section, values in creds.items()
        if isinstance(values, dict)
    }


def update_credentials_section(section: str, updates: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "upstox": {
            "access_token",
            "analytics_token",
            "token",
            "auth_code",
            "code",
            "api_key",
            "api_secret",
            "redirect_uri",
            "totp_key",
            "mobile_no",
            "pin",
            "staticip1",
            "staticip2",
        },
        "admin": {"api_key"},
    }
    if section not in allowed:
        raise ValueError(f"unsupported credentials section: {section}")

    creds = ensure_credentials()
    target = creds.setdefault(section, {})
    _merge_allowed(target, updates, allowed[section])
    save_credentials(creds)
    logging.getLogger(__name__).info("Credentials section updated: %s", section)
    return public_credentials()


def rotate_admin_api_key() -> str:
    token = new_secret_token()
    creds = ensure_credentials()
    creds.setdefault("admin", {})["api_key"] = token
    save_credentials(creds)
    logging.getLogger(__name__).warning("Admin API key rotated")
    return token


def update_strike_count(instrument: str, strike_count: int) -> dict[str, Any]:
    if strike_count < 0:
        raise ValueError("strike_count must be >= 0")
    cfg = load_config()
    backend = cfg.setdefault("backend", deepcopy(DEFAULT_BACKEND_CONFIG))
    item = backend["instruments"][normalize_instrument_name(instrument)]
    item["strike_count"] = int(strike_count)
    save_config(cfg)
    logging.getLogger(__name__).info(
        "Config changed for %s: strike_count=%s", instrument, strike_count
    )
    return deepcopy(item)


def resolve_project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_DIR / path


def db_path() -> Path:
    return resolve_project_path(app_config()["paths"]["db"])


def logs_dir() -> Path:
    return resolve_project_path(app_config()["paths"]["logs"])


def status_path() -> Path:
    return resolve_project_path(app_config()["paths"]["status"])


def ensure_runtime_dirs() -> None:
    db_path().parent.mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
    status_path().parent.mkdir(parents=True, exist_ok=True)


def timezone() -> ZoneInfo:
    return ZoneInfo(str(app_config()["timezone"]))


def now_ist() -> datetime:
    return datetime.now(timezone())


def today_ist() -> str:
    return now_ist().date().isoformat()


def iso_now() -> str:
    return now_ist().isoformat(timespec="seconds")


def parse_hhmm(value: str) -> dt_time:
    hour, minute = value.split(":", 1)
    return dt_time(int(hour), int(minute))


def market_close_reached(now: datetime | None = None) -> bool:
    current = now or now_ist()
    close_t = parse_hhmm(str(app_config()["market_close_time"]))
    close_dt = current.replace(
        hour=close_t.hour,
        minute=close_t.minute,
        second=0,
        microsecond=0,
    )
    return current >= close_dt


def market_start_datetime(day: datetime | None = None) -> datetime:
    current = day or now_ist()
    start_t = parse_hhmm(str(app_config()["market_start_time"]))
    return current.replace(
        hour=start_t.hour,
        minute=start_t.minute,
        second=0,
        microsecond=0,
    )


def market_close_datetime(day: datetime | None = None) -> datetime:
    current = day or now_ist()
    close_t = parse_hhmm(str(app_config()["market_close_time"]))
    return current.replace(
        hour=close_t.hour,
        minute=close_t.minute,
        second=0,
        microsecond=0,
    )


def is_trading_weekday(day: datetime | None = None) -> bool:
    current = day or now_ist()
    return current.weekday() < 5


def market_session_active(now: datetime | None = None) -> bool:
    current = now or now_ist()
    return (
        is_trading_weekday(current)
        and market_start_datetime(current) <= current < market_close_datetime(current)
    )


def market_session_state(now: datetime | None = None) -> str:
    current = now or now_ist()
    if not is_trading_weekday(current):
        return "closed_weekend"
    if current < market_start_datetime(current):
        return "waiting_for_open"
    if current >= market_close_datetime(current):
        return "closed"
    return "live"


def next_market_open(now: datetime | None = None) -> datetime:
    current = now or now_ist()
    candidate = market_start_datetime(current)
    if is_trading_weekday(current) and current < candidate:
        return candidate

    candidate = market_start_datetime(current + timedelta(days=1))
    while not is_trading_weekday(candidate):
        candidate = market_start_datetime(candidate + timedelta(days=1))
    return candidate


def next_collector_wakeup_iso() -> str:
    if market_session_active():
        return next_fetch_iso()
    return next_market_open().isoformat(timespec="seconds")


def next_fetch_iso() -> str:
    next_epoch = next_fetch_epoch()
    return datetime.fromtimestamp(next_epoch, timezone()).isoformat(timespec="seconds")


def next_fetch_epoch() -> float:
    interval = int(app_config()["fetch_interval_seconds"])
    now_epoch = time.time()
    return (math.floor(now_epoch / interval) + 1) * interval


def seconds_until_next_fetch() -> float:
    return max(0.0, next_fetch_epoch() - time.time())


def wait_until_next_fetch() -> None:
    time.sleep(seconds_until_next_fetch())


def idle_wait_seconds(max_seconds: float = 30.0) -> float:
    if market_session_active():
        return seconds_until_next_fetch()
    seconds = (next_market_open() - now_ist()).total_seconds()
    return max(1.0, min(max_seconds, seconds))


def wait_while_idle() -> None:
    time.sleep(idle_wait_seconds())


def http_timeout() -> int:
    return int(app_config()["http"]["timeout_seconds"])


def api_host() -> str:
    return str(app_config()["api"]["host"])


def api_port() -> int:
    return int(app_config()["api"]["port"])


def upstox_base_url() -> str:
    return str(app_config()["upstox_base_url"]).rstrip("/")


def upstox_token_url() -> str:
    return str(app_config()["upstox_token_url"])


def set_status(**updates: Any) -> None:
    try:
        if status_path().exists():
            persisted = read_json(status_path())
            STATUS.update(persisted)
    except Exception:
        logging.getLogger(__name__).exception("Failed to load persisted service status")
    STATUS.update(updates)
    STATUS["updated_at"] = iso_now()
    try:
        write_json(status_path(), STATUS)
    except Exception:
        logging.getLogger(__name__).exception("Failed to persist service status")


def service_status() -> dict[str, Any]:
    try:
        if status_path().exists():
            persisted = read_json(status_path())
            STATUS.update(persisted)
    except Exception:
        logging.getLogger(__name__).exception("Failed to load persisted service status")
    return dict(STATUS)


def configure_logging() -> None:
    ensure_runtime_dirs()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        logs_dir() / "app.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(stream)
    root.addHandler(file_handler)


def safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def option_payload(strike_row: dict[str, Any], option_type: str) -> dict[str, Any]:
    key = "call_options" if option_type.upper() == "CE" else "put_options"
    payload = strike_row.get(key) or {}
    return payload if isinstance(payload, dict) else {}


def option_market_data(strike_row: dict[str, Any], option_type: str) -> dict[str, Any]:
    payload = option_payload(strike_row, option_type)
    data = payload.get("market_data") or {}
    return data if isinstance(data, dict) else {}


def option_greeks(strike_row: dict[str, Any], option_type: str) -> dict[str, Any]:
    payload = option_payload(strike_row, option_type)
    data = payload.get("option_greeks") or {}
    return data if isinstance(data, dict) else {}


def extract_spot(strikes: list[dict[str, Any]]) -> float | None:
    for row in strikes:
        spot = safe_float(row.get("underlying_spot_price"))
        if spot is not None:
            return spot
    return None


def compute_atm(spot_price: float, instrument: str) -> float:
    step = float(instrument_config(instrument)["strike_step"])
    if step <= 0:
        raise ValueError(f"strike_step must be > 0 for {instrument}")
    return round(float(spot_price) / step) * step


def strike_range(atm_strike: float, instrument: str) -> list[float]:
    cfg = instrument_config(instrument)
    step = float(cfg["strike_step"])
    count = int(cfg["strike_count"])
    return [atm_strike + offset * step for offset in range(-count, count + 1)]


def filter_atm_window(payload: dict[str, Any]) -> dict[str, Any] | None:
    instrument = str(payload["instrument"])
    strikes = payload.get("strikes") or []
    spot = extract_spot(strikes)
    if spot is None:
        return None
    atm_strike = compute_atm(spot, instrument)
    return {
        **payload,
        "atm_strike": atm_strike,
        "strikes": [
            row for row in strikes if isinstance(row, dict)
        ],
    }


def extract_expiry_dates(contracts: list[dict[str, Any]]) -> list[str]:
    dates: set[str] = set()
    for contract in contracts:
        value = contract.get("expiry") or contract.get("expiry_date")
        if value:
            dates.add(str(value)[:10])
    return sorted(dates)


def choose_nearest_expiry(expiry_dates: list[str], now: datetime | None = None) -> str:
    if not expiry_dates:
        raise ValueError("no expiry dates available")
    current = now or now_ist()
    today = current.date()
    close_t = parse_hhmm(str(app_config()["market_close_time"]))
    for expiry in sorted(expiry_dates):
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        if expiry_date > today:
            return expiry
        if expiry_date == today and current.time() < close_t:
            return expiry
    raise ValueError("no upcoming expiry date available")


def format_option_csv_row(
    *,
    timestamp: str,
    instrument: str,
    expiry: str,
    strike_row: dict[str, Any],
    option_type: str,
) -> dict[str, Any]:
    market_data = option_market_data(strike_row, option_type)
    greeks = option_greeks(strike_row, option_type)
    return {
        "timestamp": timestamp,
        "instrument": instrument,
        "expiry": strike_row.get("expiry") or expiry,
        "underlying_key": strike_row.get("underlying_key"),
        "underlying_spot_price": strike_row.get("underlying_spot_price"),
        "strike": strike_row.get("strike_price"),
        "strike_pcr": strike_row.get("pcr"),
        "option_type": option_type,
        "option_instrument_key": option_payload(strike_row, option_type).get("instrument_key"),
        "ltp": market_data.get("ltp"),
        "oi": market_data.get("oi"),
        "prev_oi": market_data.get("prev_oi"),
        "volume": market_data.get("volume"),
        "close_price": market_data.get("close_price"),
        "bid_price": market_data.get("bid_price"),
        "bid_qty": market_data.get("bid_qty"),
        "ask_price": market_data.get("ask_price"),
        "ask_qty": market_data.get("ask_qty"),
        "iv": greeks.get("iv"),
        "delta": greeks.get("delta"),
        "gamma": greeks.get("gamma"),
        "theta": greeks.get("theta"),
        "vega": greeks.get("vega"),
        "pop": greeks.get("pop"),
    }


def row_dicts(rows: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


# ── User preferences (persisted to source/user_prefs.json) ──────────────

_user_prefs_cache: dict[str, Any] | None = None


def load_user_prefs(*, reload: bool = False) -> dict[str, Any]:
    """Return the user preference dict, populating defaults on first use."""
    global _user_prefs_cache
    if _user_prefs_cache is None or reload:
        if USER_PREFS_PATH.exists():
            try:
                _user_prefs_cache = read_json(USER_PREFS_PATH)
            except Exception:  # noqa: BLE001 - corrupt file shouldn't crash boot
                _user_prefs_cache = {}
        else:
            _user_prefs_cache = {}
        # merge in defaults for any missing keys
        for key, value in DEFAULT_USER_PREFS.items():
            _user_prefs_cache.setdefault(key, deepcopy(value))
    return _user_prefs_cache


def save_user_prefs(prefs: dict[str, Any]) -> dict[str, Any]:
    """Atomically persist user preferences and refresh the cache."""
    global _user_prefs_cache
    write_json(USER_PREFS_PATH, prefs)
    _user_prefs_cache = prefs
    return prefs


def update_user_prefs(updates: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge ``updates`` on top of the stored prefs and save."""
    if not isinstance(updates, dict):
        raise ValueError("user preferences update must be an object")
    prefs = deepcopy(load_user_prefs())
    for key, value in updates.items():
        if value is None:
            continue
        prefs[key] = value
    return save_user_prefs(prefs)
