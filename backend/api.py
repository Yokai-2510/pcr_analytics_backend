"""FastAPI read layer for the Index PCR frontend."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

import broker_api
import chart_engine
import dashboard_engine
import data_processor
import utilities as utils

app = FastAPI(title="Index PCR Backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_cache_control(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.on_event("startup")
def on_startup() -> None:
    utils.ensure_backend_config()
    utils.ensure_credentials()
    utils.configure_logging()
    data_processor.initialize_storage()
    utils.set_status(api_running=True)


@app.on_event("shutdown")
def on_shutdown() -> None:
    utils.set_status(api_running=False)


class StrikeCountPatch(BaseModel):
    strike_count: int = Field(ge=0, le=50)


class ApiSettingsPatch(BaseModel):
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)


class HttpSettingsPatch(BaseModel):
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)


class ScheduleSettingsPatch(BaseModel):
    api_mode: str | None = Field(default=None, pattern="^always$")
    weekdays: str | None = None
    daily_restart_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    worker_start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    worker_stop_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")


class InstrumentSettingsPatch(BaseModel):
    name: str | None = None
    instrument_key: str | None = None
    strike_step: int | None = Field(default=None, ge=1)
    strike_count: int | None = Field(default=None, ge=0, le=100)


class BackendConfigPatch(BaseModel):
    timezone: str | None = None
    fetch_interval_seconds: int | None = Field(default=None, ge=1, le=3600)
    market_start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    market_close_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    upstox_base_url: str | None = None
    upstox_token_url: str | None = None
    api: ApiSettingsPatch | None = None
    http: HttpSettingsPatch | None = None
    schedule: ScheduleSettingsPatch | None = None
    instruments: dict[str, InstrumentSettingsPatch] | None = None


class ChartDataRequest(BaseModel):
    instrument: str = "nifty"
    date: str | None = None
    metrics: list[str] = Field(default_factory=lambda: ["pcr"])
    baseline: str = "post_settlement"
    strike_mode: str = "aggregate"
    strikes: list[float] = Field(default_factory=list)
    strike_count: int | None = Field(default=None, ge=0, le=100)
    chart_type: str | None = None


class DataFilter(BaseModel):
    column: str
    op: str = "eq"
    value: Any = None


class DataSort(BaseModel):
    column: str
    dir: str = "asc"


class DataQueryRequest(BaseModel):
    instrument: str | None = None
    date: str | None = None
    time_from: str | None = None
    time_to: str | None = None
    columns: list[str] | None = None
    filters: list[DataFilter] = Field(default_factory=list)
    sort: list[DataSort] = Field(default_factory=list)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=500)


class SavedChartBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    config: dict[str, Any] = Field(default_factory=dict)


class UpstoxCredentialsPatch(BaseModel):
    access_token: str | None = None
    analytics_token: str | None = None
    token: str | None = None
    auth_code: str | None = None
    code: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    redirect_uri: str | None = None
    totp_key: str | None = None
    mobile_no: str | None = None
    pin: str | None = None
    staticip1: str | None = None
    staticip2: str | None = None


class AdminCredentialsPatch(BaseModel):
    api_key: str = Field(min_length=20)


def _model_dict(model: BaseModel, *, exclude_none: bool = True, exclude_unset: bool = False) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=exclude_none, exclude_unset=exclude_unset)
    return model.dict(exclude_none=exclude_none, exclude_unset=exclude_unset)


def _instrument_or_404(instrument: str) -> str:
    try:
        return utils.normalize_instrument_name(instrument)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _as_bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    expected = utils.admin_api_key()
    if not expected:
        raise HTTPException(status_code=503, detail="admin API key is not configured")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="admin token required")


@app.get("/api/pcr/{instrument}")
def pcr(
    instrument: str,
    date: str = Query(default_factory=utils.today_ist),
) -> list[dict[str, Any]]:
    return data_processor.get_pcr_series(_instrument_or_404(instrument), date)


@app.get("/api/oi-change/{instrument}")
def oi_change(
    instrument: str,
    date: str = Query(default_factory=utils.today_ist),
    baseline: str = Query("post_settlement", pattern="^(post_settlement|prev_close)$"),
) -> list[dict[str, Any]]:
    return data_processor.get_oi_change_series(_instrument_or_404(instrument), date, baseline)


@app.get("/api/total-oi/{instrument}")
def total_oi(
    instrument: str,
    date: str = Query(default_factory=utils.today_ist),
) -> list[dict[str, Any]]:
    return data_processor.get_total_oi_series(_instrument_or_404(instrument), date)


@app.get("/api/snapshots/{instrument}")
def snapshots(
    instrument: str,
    date: str = Query(default_factory=utils.today_ist),
    strike: float | None = None,
) -> list[dict[str, Any]]:
    return data_processor.get_snapshots(_instrument_or_404(instrument), date, strike)


@app.get("/api/history")
def history(instrument: str | None = None) -> list[dict[str, Any]]:
    if instrument:
        return data_processor.get_history_summary(_instrument_or_404(instrument))
    return data_processor.get_history_summary()


@app.get("/api/history/{instrument}")
def history_for_instrument(instrument: str) -> list[dict[str, Any]]:
    return data_processor.get_history_summary(_instrument_or_404(instrument))


@app.get("/api/chart/metrics")
def chart_metrics() -> dict[str, Any]:
    return chart_engine.metric_catalog()


@app.get("/api/chart/presets")
def chart_presets() -> list[dict[str, Any]]:
    return chart_engine.chart_presets()


@app.get("/api/chart/types")
def chart_types() -> list[dict[str, Any]]:
    return chart_engine.chart_types()


@app.get("/api/chart/context/{instrument}")
def chart_context(
    instrument: str,
    date: str | None = None,
) -> dict[str, Any]:
    try:
        return chart_engine.get_chart_context(_instrument_or_404(instrument), date)
    except ValueError as exc:
        raise _as_bad_request(exc) from exc


@app.post("/api/chart-data")
def chart_data(body: ChartDataRequest) -> dict[str, Any]:
    try:
        return chart_engine.get_chart_data(body.dict(exclude_none=True))
    except (KeyError, ValueError) as exc:
        raise _as_bad_request(exc) from exc


@app.get("/api/charts")
def list_charts() -> list[dict[str, Any]]:
    return chart_engine.list_saved_charts()


@app.post("/api/charts")
def create_chart(body: SavedChartBody) -> dict[str, Any]:
    return chart_engine.create_saved_chart(
        name=body.name,
        description=body.description,
        config=body.config,
    )


@app.get("/api/charts/{chart_id}")
def get_chart(chart_id: str) -> dict[str, Any]:
    chart = chart_engine.get_saved_chart(chart_id)
    if chart is None:
        raise HTTPException(status_code=404, detail="chart config not found")
    return chart


@app.put("/api/charts/{chart_id}")
def update_chart(chart_id: str, body: SavedChartBody) -> dict[str, Any]:
    chart = chart_engine.update_saved_chart(
        chart_id,
        name=body.name,
        description=body.description,
        config=body.config,
    )
    if chart is None:
        raise HTTPException(status_code=404, detail="chart config not found")
    return chart


@app.delete("/api/charts/{chart_id}")
def delete_chart(chart_id: str) -> dict[str, bool]:
    deleted = chart_engine.delete_saved_chart(chart_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="chart config not found")
    return {"deleted": True}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return utils.public_config()


@app.get("/api/config/schema")
def get_config_schema() -> dict[str, Any]:
    return {
        "editable": {
            "timezone": "string",
            "fetch_interval_seconds": "integer",
            "market_start_time": "HH:MM",
            "market_close_time": "HH:MM",
            "upstox_base_url": "url",
            "upstox_token_url": "url",
            "api": {"host": "string", "port": "integer"},
            "http": {"timeout_seconds": "integer"},
            "schedule": {
                "api_mode": "always",
                "weekdays": "systemd calendar weekday expression",
                "daily_restart_time": "HH:MM",
                "worker_start_time": "HH:MM",
                "worker_stop_time": "HH:MM",
            },
            "instruments": {
                "<instrument>": {
                    "name": "string",
                    "instrument_key": "string",
                    "strike_step": "integer",
                    "strike_count": "integer",
                }
            },
        },
        "credentials": {
            "upstox": [
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
            ],
            "admin": ["api_key"],
        },
    }


@app.patch("/api/config", dependencies=[Depends(require_admin)])
def patch_backend_config(body: BackendConfigPatch) -> dict[str, Any]:
    try:
        return utils.update_backend_config(_model_dict(body, exclude_unset=True))
    except ValueError as exc:
        raise _as_bad_request(exc) from exc


@app.patch("/api/config/{instrument}")
def patch_config(
    instrument: str,
    body: StrikeCountPatch,
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    return utils.update_strike_count(_instrument_or_404(instrument), body.strike_count)


@app.get("/api/credentials", dependencies=[Depends(require_admin)])
def get_credentials() -> dict[str, Any]:
    return utils.public_credentials()


@app.patch("/api/credentials/upstox", dependencies=[Depends(require_admin)])
def patch_upstox_credentials(body: UpstoxCredentialsPatch) -> dict[str, Any]:
    try:
        result = utils.update_credentials_section(
            "upstox",
            _model_dict(body, exclude_none=False, exclude_unset=True),
        )
        broker_api.invalidate_token()
        return result
    except ValueError as exc:
        raise _as_bad_request(exc) from exc


@app.patch("/api/credentials/admin", dependencies=[Depends(require_admin)])
def patch_admin_credentials(body: AdminCredentialsPatch) -> dict[str, Any]:
    try:
        return utils.update_credentials_section("admin", _model_dict(body, exclude_none=False))
    except ValueError as exc:
        raise _as_bad_request(exc) from exc


@app.post("/api/credentials/admin/rotate", dependencies=[Depends(require_admin)])
def rotate_admin_credentials() -> dict[str, str]:
    return {"api_key": utils.rotate_admin_api_key()}


@app.post("/api/admin/scheduler/install", dependencies=[Depends(require_admin)])
def install_scheduler() -> dict[str, bool]:
    try:
        import install_scheduler as scheduler

        scheduler.install()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"installed": True}


@app.get("/api/status")
def status() -> dict[str, Any]:
    return utils.service_status()


@app.get("/api/exchange-status/{exchange}")
def exchange_status(exchange: str = "NSE") -> dict[str, Any]:
    try:
        data = broker_api.get_exchange_status(exchange.upper())
    except broker_api.BrokerAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    utils.set_status(exchange_status=data)
    return data


# ── Dashboard summary ───────────────────────────────────────────────────

@app.get("/api/dashboard/summary")
def dashboard_summary(
    date: str | None = None,
    spark_points: int = Query(default=60, ge=2, le=500),
) -> dict[str, Any]:
    try:
        return dashboard_engine.dashboard_summary(date, spark_points=spark_points)
    except ValueError as exc:
        raise _as_bad_request(exc) from exc


# ── Flexible data browser (replaces fake "logs" UI) ─────────────────────

@app.get("/api/data/columns")
def data_columns() -> dict[str, Any]:
    return dashboard_engine.column_catalog()


@app.get("/api/data/distinct/{column}")
def data_distinct(
    column: str,
    instrument: str | None = None,
    date: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    try:
        values = dashboard_engine.distinct_values(
            column, instrument=instrument, date=date, limit=limit
        )
    except (KeyError, ValueError) as exc:
        raise _as_bad_request(exc) from exc
    return {"column": column, "values": values, "count": len(values)}


@app.post("/api/data/query")
def data_query(body: DataQueryRequest) -> dict[str, Any]:
    try:
        return dashboard_engine.query_data(_model_dict(body, exclude_none=True))
    except (KeyError, ValueError) as exc:
        raise _as_bad_request(exc) from exc


# ── Recent events from app.log (the real "logs" feed) ───────────────────

@app.get("/api/events/recent")
def events_recent(
    limit: int = Query(default=200, ge=1, le=2000),
    level: str | None = Query(default=None, pattern="^(INFO|WARNING|ERROR|DEBUG|CRITICAL)$"),
) -> dict[str, Any]:
    events = dashboard_engine.recent_events(limit=limit, level=level)
    return {"events": events, "count": len(events)}


# ── Lightweight login + persisted user preferences ──────────────────────


class LoginRequest(BaseModel):
    username: str
    password: str


class UserPrefsPatch(BaseModel):
    default_instrument: str | None = None
    default_chart_ids: list[str] | None = None
    charts_layout: str | None = None
    charts_mode: str | None = None
    last_page: str | None = None
    weekdays_enabled: list[str] | None = None


@app.post("/api/auth/login")
def auth_login(body: LoginRequest) -> dict[str, Any]:
    """Trade username + password for the admin token.

    Username is fixed to ``admin``; the password is the configured admin API
    key. Returns the api_key on success so the frontend can store it and use
    it as the ``X-Admin-Token`` header.
    """
    expected = utils.admin_api_key()
    if not expected:
        raise HTTPException(status_code=503, detail="admin API key is not configured")
    if body.username.strip().lower() != "admin":
        raise HTTPException(status_code=401, detail="invalid credentials")
    if not hmac.compare_digest(body.password, expected):
        raise HTTPException(status_code=401, detail="invalid credentials")
    return {"token": expected, "username": "admin"}


@app.get("/api/auth/verify")
def auth_verify(_: None = Depends(require_admin)) -> dict[str, bool]:
    """Cheap admin-token verification. 200 if valid, 401 otherwise."""
    return {"ok": True}


@app.get("/api/preferences")
def get_preferences() -> dict[str, Any]:
    return utils.load_user_prefs()


@app.put("/api/preferences", dependencies=[Depends(require_admin)])
def put_preferences(body: UserPrefsPatch) -> dict[str, Any]:
    try:
        return utils.update_user_prefs(_model_dict(body, exclude_unset=True))
    except (KeyError, ValueError) as exc:
        raise _as_bad_request(exc) from exc


if __name__ == "__main__":
    utils.ensure_backend_config()
    uvicorn.run(
        app,
        host=utils.api_host(),
        port=utils.api_port(),
        log_level="info",
    )
