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
    walk_forward_segments,
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
        spec = _dataset_spec(
            _training_dataset_source(payload), allow_empty_factors=True,
        )
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
        walk_forward = _resolve_walk_forward(
            payload.get("walk_forward"),
            optuna=payload.get("optuna"),
            trainable_calendar=calendar[:-horizon],
            label_horizon_trading_days=horizon,
        )

        dataset_hash = sha256(
            _canonical_json(spec).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": PREFLIGHT_SCHEMA_VERSION,
            "calendar_only": not bool(spec.get("factors")),
            "dataset": spec,
            "dataset_hash": dataset_hash,
            "segments": segments,
            "segment_session_counts": {
                name: int(
                    (
                        (calendar >= pd.Timestamp(bounds[0]))
                        & (calendar <= pd.Timestamp(bounds[1]))
                    ).sum()
                )
                for name, bounds in segments.items()
            },
            "calendar": {
                **calendar_contract,
            },
            "walk_forward": walk_forward,
        }


def _resolve_walk_forward(
    value: Any,
    *,
    optuna: Any = None,
    trainable_calendar: pd.DatetimeIndex,
    label_horizon_trading_days: int,
) -> dict[str, Any]:
    source = dict(value or {}) if isinstance(value, Mapping) else {}
    if source.get("enabled") is not True:
        return {"enabled": False}

    strategy = str(source.get("strategy") or "rolling").strip().lower()
    train_sessions = int(source.get("train_sessions") or 756)
    valid_sessions = int(source.get("valid_sessions", 60))
    test_sessions = int(source.get("test_sessions") or 20)
    step_sessions = int(source.get("step_sessions") or 20)
    embargo_sessions = int(source.get("embargo_sessions") or 5)
    if embargo_sessions < int(label_horizon_trading_days):
        raise ModelResearchError(
            "Walk-Forward隔离交易日不得小于标签预测周期"
        )

    validation_enabled = valid_sessions > 0
    optuna_source = dict(optuna or {}) if isinstance(optuna, Mapping) else {}
    optuna_enabled = optuna_source.get("enabled") is True
    if optuna_enabled and not validation_enabled:
        raise ModelResearchError("验证长度为0时不能开启Optuna")
    tuning_fold_count = (
        int(optuna_source.get("validation_windows") or 3)
        if optuna_enabled else 1
    )
    if optuna_enabled and not 2 <= tuning_fold_count <= 8:
        raise ModelResearchError("Optuna Walk-Forward调参折数必须在2到8之间")
    tuning_history_sessions = (
        (tuning_fold_count - 1) * valid_sessions
        if optuna_enabled else 0
    )
    required_history_sessions = (
        train_sessions + valid_sessions
        + embargo_sessions * (2 if validation_enabled else 1)
        + tuning_history_sessions
    )
    if len(trainable_calendar) <= required_history_sessions:
        raise ModelResearchError(
            "训练日期范围不足：首个样本外截面前至少需要"
            f"{required_history_sessions}个交易日用于训练"
            + ("、验证和双重隔离" if validation_enabled else "和单段隔离")
        )
    earliest_oos_date = trainable_calendar[
        required_history_sessions
    ].date().isoformat()

    requested_start = str(source.get("oos_date_start") or "").strip()
    requested_end = str(source.get("oos_date_end") or "").strip()
    oos_date_start = requested_start or earliest_oos_date
    oos_date_end = requested_end or trainable_calendar[-1].date().isoformat()
    if requested_start and requested_start < earliest_oos_date:
        raise ModelResearchError(
            f"Walk-Forward样本外开始日期最早为{earliest_oos_date}；"
            "该日期由训练、冻结交易日历"
            + ("、验证和双重隔离" if validation_enabled else "和单段隔离")
            + "共同确定，只能向后调整"
        )

    resolved_spec = {
        "enabled": True,
        "strategy": strategy,
        "train_sessions": train_sessions,
        "valid_sessions": valid_sessions,
        "test_sessions": test_sessions,
        "step_sessions": step_sessions,
        "embargo_sessions": embargo_sessions,
        "oos_date_start": oos_date_start,
        "oos_date_end": oos_date_end,
        "oos_date_start_mode": "manual" if requested_start else "automatic",
        "oos_date_end_mode": "manual" if requested_end else "automatic",
    }
    try:
        windows = walk_forward_segments(
            trainable_calendar,
            strategy=strategy,
            train_sessions=train_sessions,
            valid_sessions=valid_sessions,
            test_sessions=test_sessions,
            step_sessions=step_sessions,
            embargo_sessions=embargo_sessions,
            oos_date_start=oos_date_start,
            oos_date_end=oos_date_end,
        )
    except (TypeError, ValueError) as exc:
        raise ModelResearchError(str(exc)) from exc

    first_window = {
        name: list(bounds) for name, bounds in windows[0].items()
    }
    last_window = {
        name: list(bounds) for name, bounds in windows[-1].items()
    }
    window_timeline = [
        _walk_forward_window_timeline(
            trainable_calendar,
            window,
            index=index,
        )
        for index, window in enumerate(windows, start=1)
    ]
    prediction_start_index = int(
        trainable_calendar.get_loc(pd.Timestamp(oos_date_start))
    )
    prediction_end_index = int(
        trainable_calendar.get_loc(pd.Timestamp(oos_date_end))
    )
    backtest_date_start = (
        trainable_calendar[prediction_start_index + 1].date().isoformat()
        if prediction_start_index + 1 < len(trainable_calendar)
        else ""
    )

    return {
        "enabled": True,
        "schema_version": "alphablocks.walk-forward-preflight.v1",
        "spec": resolved_spec,
        "earliest_oos_date": earliest_oos_date,
        "required_history_sessions": required_history_sessions,
        "optuna_tuning_fold_count": tuning_fold_count if optuna_enabled else 0,
        "optuna_extra_history_sessions": tuning_history_sessions,
        "prediction_date_start": oos_date_start,
        "prediction_date_end": oos_date_end,
        "prediction_session_count": prediction_end_index - prediction_start_index + 1,
        "backtest_date_start": backtest_date_start,
        "backtest_date_end": oos_date_end,
        "window_count": len(windows),
        "first_window": first_window,
        "last_window": last_window,
        "windows": window_timeline,
    }


def _walk_forward_window_timeline(
    calendar: pd.DatetimeIndex,
    window: Mapping[str, tuple[str, str]],
    *,
    index: int,
) -> dict[str, Any]:
    normalized = pd.DatetimeIndex(calendar).normalize()

    def segment(name: str) -> dict[str, Any]:
        bounds = window[name]
        start = pd.Timestamp(bounds[0]).normalize()
        end = pd.Timestamp(bounds[1]).normalize()
        return {
            "date_start": start.date().isoformat(),
            "date_end": end.date().isoformat(),
            "sessions": int(((normalized >= start) & (normalized <= end)).sum()),
        }

    def embargo(left: str, right: str) -> dict[str, Any]:
        left_end = pd.Timestamp(window[left][1]).normalize()
        right_start = pd.Timestamp(window[right][0]).normalize()
        left_index = int(normalized.get_loc(left_end))
        right_index = int(normalized.get_loc(right_start))
        sessions = max(0, right_index - left_index - 1)
        if sessions == 0:
            return {"date_start": "", "date_end": "", "sessions": 0}
        return {
            "date_start": normalized[left_index + 1].date().isoformat(),
            "date_end": normalized[right_index - 1].date().isoformat(),
            "sessions": sessions,
        }

    if "valid" not in window:
        empty = {"date_start": "", "date_end": "", "sessions": 0}
        return {
            "index": int(index),
            "train": segment("train"),
            "train_valid_embargo": dict(empty),
            "valid": dict(empty),
            "valid_test_embargo": dict(empty),
            "train_test_embargo": embargo("train", "test"),
            "test": segment("test"),
        }
    return {
        "index": int(index),
        "train": segment("train"),
        "train_valid_embargo": embargo("train", "valid"),
        "valid": segment("valid"),
        "valid_test_embargo": embargo("valid", "test"),
        "test": segment("test"),
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
