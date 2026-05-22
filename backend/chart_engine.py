"""Config-driven chart data and saved chart helpers."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import data_processor
import utilities as utils

BASELINES = {"post_settlement", "prev_close", "market_open"}
BASELINE_DB_ALIAS: dict[str, str] = {}  # no aliases needed; all are real types
BASELINE_LABELS = [
    {"id": "prev_close", "label": "Previous Day Close", "description": "Option chain captured before market open (08:55) — previous day's closing OI."},
    {"id": "market_open", "label": "Market Open", "description": "First snapshot exactly at market open (09:15:00)."},
    {"id": "post_settlement", "label": "Post Settlement", "description": "Snapshot after initial settlement / first full tick."},
]
STRIKE_MODES = {"aggregate", "atm_window", "custom", "all"}


@dataclass(frozen=True)
class MetricDef:
    id: str
    label: str
    group: str
    description: str
    unit: str
    color: str
    axis: str
    aggregate_sql: str
    strike_sql: str
    requires_baseline: bool = False

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("aggregate_sql")
        data.pop("strike_sql")
        return data


def _change_sum(column: str) -> str:
    return (
        f"SUM(CASE WHEN b.{column} IS NULL THEN NULL "
        f"ELSE COALESCE(s.{column}, 0) - COALESCE(b.{column}, 0) END)"
    )


def _change_avg(column: str) -> str:
    return (
        f"AVG(CASE WHEN b.{column} IS NULL THEN NULL "
        f"ELSE COALESCE(s.{column}, 0) - COALESCE(b.{column}, 0) END)"
    )


def _change_pct(column: str) -> str:
    return (
        f"CASE WHEN SUM(COALESCE(b.{column}, 0)) > 0 THEN "
        f"100.0 * {_change_sum(column)} / SUM(COALESCE(b.{column}, 0)) "
        "ELSE NULL END"
    )


METRICS: dict[str, MetricDef] = {
    "underlying_spot_price": MetricDef(
        id="underlying_spot_price",
        label="Underlying Spot",
        group="price",
        description="Average underlying spot price for each timestamp.",
        unit="price",
        color="#94a3b8",
        axis="right",
        aggregate_sql="AVG(s.underlying_spot_price)",
        strike_sql="AVG(s.underlying_spot_price)",
    ),
    "pcr": MetricDef(
        id="pcr",
        label="Put-Call Ratio",
        group="ratio",
        description="Put open interest divided by call open interest.",
        unit="ratio",
        color="#22c55e",
        axis="left",
        aggregate_sql=(
            "CASE WHEN SUM(COALESCE(s.ce_oi, 0)) > 0 THEN "
            "SUM(COALESCE(s.pe_oi, 0)) / SUM(COALESCE(s.ce_oi, 0)) "
            "ELSE NULL END"
        ),
        strike_sql=(
            "CASE WHEN AVG(COALESCE(s.ce_oi, 0)) > 0 THEN "
            "AVG(COALESCE(s.pe_oi, 0)) / AVG(COALESCE(s.ce_oi, 0)) "
            "ELSE NULL END"
        ),
    ),
    "total_ce_oi": MetricDef(
        id="total_ce_oi",
        label="Call OI",
        group="open_interest",
        description="Total call open interest.",
        unit="contracts",
        color="#4ade80",
        axis="left",
        aggregate_sql="SUM(COALESCE(s.ce_oi, 0))",
        strike_sql="AVG(s.ce_oi)",
    ),
    "total_pe_oi": MetricDef(
        id="total_pe_oi",
        label="Put OI",
        group="open_interest",
        description="Total put open interest.",
        unit="contracts",
        color="#f87171",
        axis="left",
        aggregate_sql="SUM(COALESCE(s.pe_oi, 0))",
        strike_sql="AVG(s.pe_oi)",
    ),
    "ce_oi_change": MetricDef(
        id="ce_oi_change",
        label="Call OI Change",
        group="open_interest",
        description="Call open interest change from the selected baseline.",
        unit="contracts",
        color="#22c55e",
        axis="left",
        aggregate_sql=_change_sum("ce_oi"),
        strike_sql=_change_avg("ce_oi"),
        requires_baseline=True,
    ),
    "pe_oi_change": MetricDef(
        id="pe_oi_change",
        label="Put OI Change",
        group="open_interest",
        description="Put open interest change from the selected baseline.",
        unit="contracts",
        color="#ef4444",
        axis="left",
        aggregate_sql=_change_sum("pe_oi"),
        strike_sql=_change_avg("pe_oi"),
        requires_baseline=True,
    ),
    "ce_oi_change_pct": MetricDef(
        id="ce_oi_change_pct",
        label="Call OI Change %",
        group="open_interest",
        description="Call open interest percentage change from the selected baseline.",
        unit="percent",
        color="#86efac",
        axis="left",
        aggregate_sql=_change_pct("ce_oi"),
        strike_sql=(
            "CASE WHEN AVG(COALESCE(b.ce_oi, 0)) > 0 THEN "
            f"100.0 * {_change_avg('ce_oi')} / AVG(COALESCE(b.ce_oi, 0)) "
            "ELSE NULL END"
        ),
        requires_baseline=True,
    ),
    "pe_oi_change_pct": MetricDef(
        id="pe_oi_change_pct",
        label="Put OI Change %",
        group="open_interest",
        description="Put open interest percentage change from the selected baseline.",
        unit="percent",
        color="#fca5a5",
        axis="left",
        aggregate_sql=_change_pct("pe_oi"),
        strike_sql=(
            "CASE WHEN AVG(COALESCE(b.pe_oi, 0)) > 0 THEN "
            f"100.0 * {_change_avg('pe_oi')} / AVG(COALESCE(b.pe_oi, 0)) "
            "ELSE NULL END"
        ),
        requires_baseline=True,
    ),
    "ce_volume": MetricDef(
        id="ce_volume",
        label="Call Volume",
        group="volume",
        description="Total call volume.",
        unit="contracts",
        color="#38bdf8",
        axis="left",
        aggregate_sql="SUM(COALESCE(s.ce_volume, 0))",
        strike_sql="AVG(s.ce_volume)",
    ),
    "pe_volume": MetricDef(
        id="pe_volume",
        label="Put Volume",
        group="volume",
        description="Total put volume.",
        unit="contracts",
        color="#fb7185",
        axis="left",
        aggregate_sql="SUM(COALESCE(s.pe_volume, 0))",
        strike_sql="AVG(s.pe_volume)",
    ),
    "volume_pcr": MetricDef(
        id="volume_pcr",
        label="Volume PCR",
        group="ratio",
        description="Put volume divided by call volume.",
        unit="ratio",
        color="#a78bfa",
        axis="left",
        aggregate_sql=(
            "CASE WHEN SUM(COALESCE(s.ce_volume, 0)) > 0 THEN "
            "SUM(COALESCE(s.pe_volume, 0)) / SUM(COALESCE(s.ce_volume, 0)) "
            "ELSE NULL END"
        ),
        strike_sql=(
            "CASE WHEN AVG(COALESCE(s.ce_volume, 0)) > 0 THEN "
            "AVG(COALESCE(s.pe_volume, 0)) / AVG(COALESCE(s.ce_volume, 0)) "
            "ELSE NULL END"
        ),
    ),
    "ce_ltp": MetricDef(
        id="ce_ltp",
        label="Call LTP",
        group="price",
        description="Average call last traded price.",
        unit="price",
        color="#2dd4bf",
        axis="left",
        aggregate_sql="AVG(s.ce_ltp)",
        strike_sql="AVG(s.ce_ltp)",
    ),
    "pe_ltp": MetricDef(
        id="pe_ltp",
        label="Put LTP",
        group="price",
        description="Average put last traded price.",
        unit="price",
        color="#f97316",
        axis="left",
        aggregate_sql="AVG(s.pe_ltp)",
        strike_sql="AVG(s.pe_ltp)",
    ),
    "ce_iv": MetricDef(
        id="ce_iv",
        label="Call IV",
        group="greeks",
        description="Average call implied volatility.",
        unit="percent",
        color="#60a5fa",
        axis="left",
        aggregate_sql="AVG(s.ce_iv)",
        strike_sql="AVG(s.ce_iv)",
    ),
    "pe_iv": MetricDef(
        id="pe_iv",
        label="Put IV",
        group="greeks",
        description="Average put implied volatility.",
        unit="percent",
        color="#f472b6",
        axis="left",
        aggregate_sql="AVG(s.pe_iv)",
        strike_sql="AVG(s.pe_iv)",
    ),
    "delta_pcr": MetricDef(
        id="delta_pcr",
        label="ΔPCR (OI Change)",
        group="ratio",
        description="Ratio of change in PE OI to change in CE OI from the selected baseline. Values above 1 indicate puts gaining OI faster than calls.",
        unit="ratio",
        color="#c084fc",
        axis="left",
        aggregate_sql=(
            "CASE WHEN SUM(COALESCE(s.ce_oi, 0) - COALESCE(b.ce_oi, 0)) > 0 THEN "
            "SUM(COALESCE(s.pe_oi, 0) - COALESCE(b.pe_oi, 0)) / "
            "SUM(COALESCE(s.ce_oi, 0) - COALESCE(b.ce_oi, 0)) "
            "ELSE NULL END"
        ),
        strike_sql=(
            "CASE WHEN (COALESCE(s.ce_oi, 0) - COALESCE(b.ce_oi, 0)) > 0 THEN "
            "(COALESCE(s.pe_oi, 0) - COALESCE(b.pe_oi, 0)) / "
            "(COALESCE(s.ce_oi, 0) - COALESCE(b.ce_oi, 0)) "
            "ELSE NULL END"
        ),
        requires_baseline=True,
    ),
    "ce_oi_cumm": MetricDef(
        id="ce_oi_cumm",
        label="CE Cumulative OI Change",
        group="open_interest",
        description="Cumulative call OI change from first tick of the day.",
        unit="contracts",
        color="#86efac",
        axis="left",
        aggregate_sql=(
            "SUM(COALESCE(s.ce_oi, 0)) - COALESCE("
            "(SELECT SUM(COALESCE(f.ce_oi, 0)) FROM oi_snapshots f "
            "WHERE f.instrument = s.instrument AND f.timestamp = "
            "(SELECT MIN(timestamp) FROM oi_snapshots "
            "WHERE instrument = s.instrument AND substr(timestamp, 1, 10) = substr(s.timestamp, 1, 10))), 0)"
        ),
        strike_sql="AVG(s.ce_oi)",
    ),
    "pe_oi_cumm": MetricDef(
        id="pe_oi_cumm",
        label="PE Cumulative OI Change",
        group="open_interest",
        description="Cumulative put OI change from first tick of the day.",
        unit="contracts",
        color="#fca5a5",
        axis="left",
        aggregate_sql=(
            "SUM(COALESCE(s.pe_oi, 0)) - COALESCE("
            "(SELECT SUM(COALESCE(f.pe_oi, 0)) FROM oi_snapshots f "
            "WHERE f.instrument = s.instrument AND f.timestamp = "
            "(SELECT MIN(timestamp) FROM oi_snapshots "
            "WHERE instrument = s.instrument AND substr(timestamp, 1, 10) = substr(s.timestamp, 1, 10))), 0)"
        ),
        strike_sql="AVG(s.pe_oi)",
    ),
}


CHART_PRESETS: list[dict[str, Any]] = [
    {
        "id": "pcr-with-spot",
        "name": "Put-Call Ratio",
        "description": "PCR with underlying spot overlay.",
        "config": {
            "instrument": "nifty",
            "metrics": ["pcr", "underlying_spot_price"],
            "strike_mode": "aggregate",
            "baseline": "post_settlement",
            "chart_type": "line",
        },
    },
    {
        "id": "oi-change-call-vs-put",
        "name": "OI Change Call vs Put",
        "description": "Call and put OI change from the selected baseline.",
        "config": {
            "instrument": "nifty",
            "metrics": ["ce_oi_change", "pe_oi_change", "underlying_spot_price"],
            "strike_mode": "aggregate",
            "baseline": "post_settlement",
            "chart_type": "line",
        },
    },
    {
        "id": "total-oi-call-vs-put",
        "name": "Total OI Call vs Put",
        "description": "Total call OI and put OI.",
        "config": {
            "instrument": "nifty",
            "metrics": ["total_ce_oi", "total_pe_oi", "underlying_spot_price"],
            "strike_mode": "aggregate",
            "baseline": "post_settlement",
            "chart_type": "line",
        },
    },
    {
        "id": "volume-call-vs-put",
        "name": "Volume Call vs Put",
        "description": "Total call and put volume.",
        "config": {
            "instrument": "nifty",
            "metrics": ["ce_volume", "pe_volume"],
            "strike_mode": "aggregate",
            "baseline": "post_settlement",
            "chart_type": "bar",
        },
    },
    {
        "id": "multi-strike-oi-change",
        "name": "Multi-Strike OI Change",
        "description": "Strike-level OI changes around ATM.",
        "config": {
            "instrument": "nifty",
            "metrics": ["ce_oi_change", "pe_oi_change"],
            "strike_mode": "atm_window",
            "strike_count": 5,
            "baseline": "post_settlement",
            "chart_type": "line",
        },
    },
    {
        "id": "strike-volume",
        "name": "Strike Volume",
        "description": "Strike-level call and put volume around ATM.",
        "config": {
            "instrument": "nifty",
            "metrics": ["ce_volume", "pe_volume"],
            "strike_mode": "atm_window",
            "strike_count": 5,
            "baseline": "post_settlement",
            "chart_type": "line",
        },
    },
]


CHART_TYPES: list[dict[str, Any]] = [
    {
        "id": "line",
        "label": "Line",
        "description": "Time series line chart, best for ratios and trends.",
        "supports_multi_series": True,
        "supports_strike_dimension": True,
    },
    {
        "id": "area",
        "label": "Area",
        "description": "Filled line chart, useful for cumulative volume / OI.",
        "supports_multi_series": True,
        "supports_strike_dimension": True,
    },
    {
        "id": "bar",
        "label": "Bar",
        "description": "Bars per timestamp, ideal for OI change vs baseline.",
        "supports_multi_series": True,
        "supports_strike_dimension": True,
    },
    {
        "id": "candle",
        "label": "Candle",
        "description": "OHLC candle chart for the underlying spot (per-minute).",
        "supports_multi_series": False,
        "supports_strike_dimension": False,
    },
    {
        "id": "heatmap",
        "label": "Heatmap",
        "description": "Strike x time matrix of any per-strike metric.",
        "supports_multi_series": False,
        "supports_strike_dimension": True,
    },
    {
        "id": "scatter",
        "label": "Scatter",
        "description": "Scatter plot, e.g. IV vs strike, volume vs OI.",
        "supports_multi_series": True,
        "supports_strike_dimension": True,
    },
    {
        "id": "histogram",
        "label": "Histogram",
        "description": "Distribution of any numeric metric across strikes / time.",
        "supports_multi_series": False,
        "supports_strike_dimension": True,
    },
]


def metric_catalog() -> dict[str, Any]:
    return {
        "metrics": [metric.public() for metric in METRICS.values()],
        # Keep the legacy flat list (used by older clients) plus a richer label
        # list for new UIs that want friendly names.
        "baselines": sorted(BASELINES),
        "baseline_labels": BASELINE_LABELS,
        "strike_modes": sorted(STRIKE_MODES),
        "chart_types": CHART_TYPES,
    }


def chart_types() -> list[dict[str, Any]]:
    return CHART_TYPES


def chart_presets() -> list[dict[str, Any]]:
    return CHART_PRESETS


def _normalize_metrics(metrics: Any) -> list[str]:
    if not metrics:
        return ["pcr"]
    if not isinstance(metrics, list):
        raise ValueError("metrics must be a list")
    normalized = [str(metric) for metric in metrics]
    invalid = [metric for metric in normalized if metric not in METRICS]
    if invalid:
        raise ValueError(f"unknown chart metric(s): {', '.join(invalid)}")
    return normalized


def _normalize_chart_config(config: dict[str, Any]) -> dict[str, Any]:
    instrument = utils.normalize_instrument_name(str(config.get("instrument") or "nifty"))
    date = str(config.get("date") or utils.today_ist())[:10]
    baseline = str(config.get("baseline") or "post_settlement")
    if baseline not in BASELINES:
        raise ValueError(f"baseline must be one of: {', '.join(sorted(BASELINES))}")

    strike_mode = str(config.get("strike_mode") or "aggregate")
    if strike_mode not in STRIKE_MODES:
        raise ValueError(f"strike_mode must be one of: {', '.join(sorted(STRIKE_MODES))}")

    strikes = config.get("strikes") or []
    if not isinstance(strikes, list):
        raise ValueError("strikes must be a list")

    strike_count = config.get("strike_count")
    if strike_count is not None:
        strike_count = int(strike_count)
        if strike_count < 0 or strike_count > 100:
            raise ValueError("strike_count must be between 0 and 100")

    return {
        **config,
        "instrument": instrument,
        "date": date,
        "baseline": baseline,
        "metrics": _normalize_metrics(config.get("metrics")),
        "strike_mode": strike_mode,
        "strikes": [float(strike) for strike in strikes],
        "strike_count": strike_count,
    }


def _baseline_join() -> str:
    return """
        LEFT JOIN daily_baselines b
            ON b.date = ?
            AND b.baseline_type = ?
            AND b.instrument = s.instrument
            AND b.expiry = s.expiry
            AND b.strike = s.strike
    """


def _resolve_baseline_db(baseline: str) -> str:
    """Map UI baseline name to the stored ``baseline_type`` value."""
    return BASELINE_DB_ALIAS.get(baseline, baseline)


def _query_aggregate(config: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = config["metrics"]
    select_metrics = ",\n            ".join(
        f"{METRICS[metric].aggregate_sql} AS {metric}" for metric in metrics
    )
    query = f"""
        SELECT
            s.timestamp,
            {select_metrics}
        FROM oi_snapshots s
        {_baseline_join()}
        WHERE s.instrument = ? AND substr(s.timestamp, 1, 10) = ?
          AND s.timestamp IN ({data_processor.MINUTE_FILTER_SQL})
        GROUP BY s.timestamp
        ORDER BY s.timestamp
    """
    params = (
        config["date"],
        _resolve_baseline_db(config["baseline"]),
        config["instrument"],
        config["date"],
        config["instrument"],
        config["date"],
    )
    return data_processor._query(query, params)


def _query_strikes(config: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = config["metrics"]
    select_metrics = ",\n            ".join(
        f"{METRICS[metric].strike_sql} AS {metric}" for metric in metrics
    )
    where = [
        "s.instrument = ?",
        "substr(s.timestamp, 1, 10) = ?",
        f"s.timestamp IN ({data_processor.MINUTE_FILTER_SQL})",
    ]
    params: list[Any] = [
        config["date"],
        _resolve_baseline_db(config["baseline"]),
        config["instrument"],
        config["date"],
        config["instrument"],
        config["date"],
    ]

    if config["strike_mode"] == "custom":
        strikes = config["strikes"]
        if not strikes:
            raise ValueError("custom strike_mode requires at least one strike")
        placeholders = ", ".join("?" for _ in strikes)
        where.append(f"s.strike IN ({placeholders})")
        params.extend(strikes)
    elif config["strike_mode"] == "atm_window":
        instrument_cfg = utils.instrument_config(config["instrument"])
        strike_count = config["strike_count"]
        if strike_count is None:
            strike_count = int(instrument_cfg["strike_count"])
        width = float(instrument_cfg["strike_step"]) * float(strike_count)
        where.append("ABS(s.strike - s.atm_strike) <= ?")
        params.append(width)

    query = f"""
        SELECT
            s.timestamp,
            s.strike,
            AVG(s.atm_strike) AS atm_strike,
            {select_metrics}
        FROM oi_snapshots s
        {_baseline_join()}
        WHERE {" AND ".join(where)}
        GROUP BY s.timestamp, s.strike
        ORDER BY s.timestamp, s.strike
    """
    return data_processor._query(query, tuple(params))


def _format_strike(strike: Any) -> str:
    value = utils.safe_float(strike)
    if value is None:
        return str(strike)
    return f"{value:g}"


def _build_series(config: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    if config["strike_mode"] in ("aggregate", "all"):
        for metric_id in config["metrics"]:
            metric = METRICS[metric_id]
            series.append(
                {
                    "key": metric_id,
                    "metric": metric_id,
                    "label": metric.label,
                    "unit": metric.unit,
                    "axis": metric.axis,
                    "color": metric.color,
                    "points": [
                        {"timestamp": row["timestamp"], "value": row.get(metric_id)}
                        for row in rows
                    ],
                }
            )
        return series

    strikes = sorted({row["strike"] for row in rows if row.get("strike") is not None})
    for metric_id in config["metrics"]:
        metric = METRICS[metric_id]
        for strike in strikes:
            strike_label = _format_strike(strike)
            series.append(
                {
                    "key": f"{metric_id}:{strike_label}",
                    "metric": metric_id,
                    "strike": strike,
                    "label": f"{metric.label} {strike_label}",
                    "unit": metric.unit,
                    "axis": metric.axis,
                    "color": metric.color,
                    "points": [
                        {"timestamp": row["timestamp"], "value": row.get(metric_id)}
                        for row in rows
                        if row.get("strike") == strike
                    ],
                }
            )
    return series


def get_chart_data(config: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_chart_config(config)
    if normalized["strike_mode"] in ("aggregate", "all"):
        rows = _query_aggregate(normalized)
    else:
        rows = _query_strikes(normalized)

    return {
        "config": normalized,
        "metrics": [METRICS[metric].public() for metric in normalized["metrics"]],
        "rows": rows,
        "series": _build_series(normalized, rows),
        "meta": {
            "row_count": len(rows),
            "generated_at": utils.iso_now(),
            "available_strikes": sorted(
                {row["strike"] for row in rows if row.get("strike") is not None}
            ),
        },
    }


def get_chart_context(instrument: str, date: str | None = None) -> dict[str, Any]:
    name = utils.normalize_instrument_name(instrument)
    with data_processor.connect() as conn:
        dates = [
            row["date"]
            for row in conn.execute(
                """
                SELECT DISTINCT substr(timestamp, 1, 10) AS date
                FROM oi_snapshots
                WHERE instrument = ?
                ORDER BY date DESC
                """,
                (name,),
            ).fetchall()
        ]
        selected_date = (date or dates[0] if dates else date or utils.today_ist())[:10]
        strikes = [
            row["strike"]
            for row in conn.execute(
                """
                SELECT DISTINCT strike
                FROM oi_snapshots
                WHERE instrument = ? AND substr(timestamp, 1, 10) = ?
                ORDER BY strike
                """,
                (name, selected_date),
            ).fetchall()
        ]
        baselines = [
            row["baseline_type"]
            for row in conn.execute(
                """
                SELECT DISTINCT baseline_type
                FROM daily_baselines
                WHERE instrument = ? AND date = ?
                ORDER BY baseline_type
                """,
                (name, selected_date),
            ).fetchall()
        ]
    # surface the full label list (post_settlement always works because the
    # worker freezes it on the first tick of the day; market_open is an alias)
    return {
        "instrument": name,
        "date": selected_date,
        "dates": dates,
        "strikes": strikes,
        "baselines": baselines,
        "baseline_labels": BASELINE_LABELS,
        "default_baseline": "post_settlement",
    }


def _chart_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    try:
        data["config"] = json.loads(data.pop("config_json"))
    except json.JSONDecodeError:
        data["config"] = {}
    return data


def list_saved_charts() -> list[dict[str, Any]]:
    with data_processor.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, description, config_json, created_at, updated_at
            FROM chart_configs
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [_chart_row(row) for row in rows]


def get_saved_chart(chart_id: str) -> dict[str, Any] | None:
    with data_processor.connect() as conn:
        row = conn.execute(
            """
            SELECT id, name, description, config_json, created_at, updated_at
            FROM chart_configs
            WHERE id = ?
            """,
            (chart_id,),
        ).fetchone()
    return _chart_row(row) if row else None


def create_saved_chart(
    *,
    name: str,
    config: dict[str, Any],
    description: str | None = None,
) -> dict[str, Any]:
    chart_id = uuid.uuid4().hex
    now = utils.iso_now()
    with data_processor.connect() as conn:
        conn.execute(
            """
            INSERT INTO chart_configs
                (id, name, description, config_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chart_id, name, description, json.dumps(config), now, now),
        )
    created = get_saved_chart(chart_id)
    if created is None:
        raise RuntimeError("saved chart was not created")
    return created


def update_saved_chart(
    chart_id: str,
    *,
    name: str,
    config: dict[str, Any],
    description: str | None = None,
) -> dict[str, Any] | None:
    with data_processor.connect() as conn:
        result = conn.execute(
            """
            UPDATE chart_configs
            SET name = ?, description = ?, config_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (name, description, json.dumps(config), utils.iso_now(), chart_id),
        )
    if result.rowcount == 0:
        return None
    return get_saved_chart(chart_id)


def delete_saved_chart(chart_id: str) -> bool:
    with data_processor.connect() as conn:
        result = conn.execute("DELETE FROM chart_configs WHERE id = ?", (chart_id,))
    return result.rowcount > 0
