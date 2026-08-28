from __future__ import annotations

from hashlib import sha256
import re
from typing import Any, Callable, Mapping

import pandas as pd

from factor_service.model_research_repository import (
    ModelResearchError,
    _canonical_json,
    _dataset_spec,
    _training_dataset_source,
)
from factor_service.research.config import Settings, load_settings
from factor_service.research.data_binding_source import (
    load_bound_trading_calendar,
)
from factor_service.research.dataset import (
    resolve_dataset_split,
)
from factor_service.research.training_resource_settings import (
    TRADING_CALENDAR_BINDING_ID,
    frozen_data_binding,
)


PREFLIGHT_SCHEMA_VERSION = "alphablocks.dataset-preflight.v1"
CalendarLoader = Callable[[Mapping[str, Any], Settings], pd.DatetimeIndex]


class DatasetPreflightService:
    """Validate a frozen Dataset contract without materializing X/y.

    Only the trading calendar is loaded.  Feature values, membership rows,
    labels, transforms and Parquet snapshots remain part of DatasetBuild.
    """

    def __init__(
        self,
        *,
        settings_loader: Callable[[], Settings] = load_settings,
        calendar_loader: CalendarLoader | None = None,
    ) -> None:
        self._settings_loader = settings_loader
        self._calendar_loader = calendar_loader or _load_trading_calendar

    def validate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        spec = _dataset_spec(_training_dataset_source(payload))
        settings = self._settings_loader()
        calendar = _normalized_calendar(
            self._calendar_loader(spec, settings),
            date_start=str(spec["date_start"]),
            date_end=str(spec["date_end"]),
        )
        if calendar.empty:
            raise ModelResearchError("训练日期范围内没有有效交易日")

        horizon = int(
            dict(spec.get("label") or {}).get("horizon_trading_days") or 0
        )
        if horizon < 1:
            raise ModelResearchError("标签预测周期必须至少为1个交易日")
        if len(calendar) <= horizon:
            raise ModelResearchError(
                f"训练日期范围不足以生成{horizon}交易日标签"
            )
        split = dict(spec.get("split") or {})
        try:
            resolution = resolve_dataset_split(
                calendar,
                split=split,
                label_horizon_trading_days=horizon,
            )
        except (TypeError, ValueError) as exc:
            raise ModelResearchError(str(exc)) from exc
        existing_resolution = split.get("resolved")
        if existing_resolution is not None and existing_resolution != resolution:
            raise ModelResearchError(
                "冻结交易日历或切分边界已漂移，必须重新验证研究计划"
            )
        split["resolved"] = resolution
        spec["split"] = split
        segments = dict(resolution["segments"])
        calendar_contract = dict(resolution["calendar"])

        dataset_hash = sha256(
            _canonical_json(spec).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": PREFLIGHT_SCHEMA_VERSION,
            "dataset": spec,
            "dataset_hash": dataset_hash,
            "segments": segments,
            "calendar": {
                **calendar_contract,
            },
        }


def _normalized_calendar(
    dates: Any,
    *,
    date_start: str,
    date_end: str,
) -> pd.DatetimeIndex:
    calendar = pd.DatetimeIndex(
        pd.to_datetime(pd.Index(dates), errors="coerce")
    ).dropna()
    calendar = pd.DatetimeIndex(calendar.normalize().unique()).sort_values()
    return calendar[
        (calendar >= pd.Timestamp(date_start))
        & (calendar <= pd.Timestamp(date_end))
    ]


def _load_trading_calendar(
    spec: Mapping[str, Any], settings: Settings,
) -> pd.DatetimeIndex:
    date_start = str(spec["date_start"])
    date_end = str(spec["date_end"])
    binding = frozen_data_binding(
        spec.get("data_bindings"), TRADING_CALENDAR_BINDING_ID,
    )
    if binding is not None:
        return load_bound_trading_calendar(
            settings, binding, date_start, date_end,
        )

    # Legacy deployments without v2 resource bindings use the same index-price
    # calendar source as DatasetBuilder.  This query does not load constituents,
    # features, labels or transformations.
    database = str(settings.source_database or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
        raise ModelResearchError("source_database不是安全的ClickHouse标识符")
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        autogenerate_session_id=False,
    )
    calendar_code = (
        "000905.SH"
        if str(spec.get("universe_id") or "") == "all_a"
        else str(spec.get("index_code") or "000905.SH")
    )
    rows = client.query(
        f"""
        SELECT DISTINCT toDate(trade_time) AS trade_date
        FROM {database}.ad_market_kline_daily
        WHERE code = {{calendar_code:String}}
          AND toDate(trade_time) >= {{date_start:Date}}
          AND toDate(trade_time) <= {{date_end:Date}}
        ORDER BY trade_date
        """,
        parameters={
            "calendar_code": calendar_code,
            "date_start": date_start,
            "date_end": date_end,
        },
    ).result_rows
    return pd.DatetimeIndex([row[0] for row in rows])


__all__ = ["DatasetPreflightService", "PREFLIGHT_SCHEMA_VERSION"]
