from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from factor_service.research.training_resource_settings import (
    CONFIGURED_STOCK_POOL_SOURCE_SCHEMA_VERSION,
)


SIZE_ROTATION_FEATURE_SCHEMA_VERSION = "alphablocks.size-rotation-feature.v1"
SIZE_ROTATION_FEATURE_NAMES: tuple[str, ...] = (
    "size_float_style",
    "size_stock_momentum_interaction",
    "size_rotation_regime_interaction",
    "size_large_momentum_regime_interaction",
)


def normalize_size_rotation_feature(
    source: Mapping[str, Any] | None,
    *,
    default_enabled: bool,
) -> dict[str, Any]:
    raw = dict(source or {})
    allowed = {
        "schema_version",
        "enabled",
        "large_pool",
        "small_pool",
        "return_window",
        "basket_size",
        "regime_window",
        "feature_names",
        "point_in_time",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(
            "size_rotation_feature包含未支持字段: " + ", ".join(unknown)
        )
    enabled = raw.get("enabled", default_enabled)
    if not isinstance(enabled, bool):
        raise ValueError("size_rotation_feature.enabled必须是布尔值")
    schema_version = str(
        raw.get("schema_version") or SIZE_ROTATION_FEATURE_SCHEMA_VERSION
    ).strip()
    if schema_version != SIZE_ROTATION_FEATURE_SCHEMA_VERSION:
        raise ValueError("size_rotation_feature.schema_version不受支持")
    return_window = _bounded_int(
        raw.get("return_window", 10),
        "size_rotation_feature.return_window",
        minimum=2,
        maximum=60,
    )
    basket_size = _bounded_int(
        raw.get("basket_size", 20),
        "size_rotation_feature.basket_size",
        minimum=5,
        maximum=100,
    )
    regime_window = _bounded_int(
        raw.get("regime_window", 60),
        "size_rotation_feature.regime_window",
        minimum=20,
        maximum=252,
    )
    feature_names = list(SIZE_ROTATION_FEATURE_NAMES)
    configured_names = raw.get("feature_names")
    if configured_names is not None and configured_names != feature_names:
        raise ValueError("size_rotation_feature.feature_names当前只支持固定特征集合")
    point_in_time = raw.get("point_in_time", True)
    if point_in_time is not True:
        raise ValueError("size_rotation_feature.point_in_time必须为true")

    large_pool = _normalize_pool(raw.get("large_pool"), enabled=enabled)
    small_pool = _normalize_pool(raw.get("small_pool"), enabled=enabled)
    if enabled and large_pool["source_id"] == small_pool["source_id"]:
        raise ValueError("大小盘轮动的大盘池和小盘池不能相同")
    return {
        "schema_version": schema_version,
        "enabled": enabled,
        "large_pool": large_pool,
        "small_pool": small_pool,
        "return_window": return_window,
        "basket_size": basket_size,
        "regime_window": regime_window,
        "feature_names": feature_names,
        "point_in_time": True,
    }


def size_rotation_feature_names(
    config: Mapping[str, Any] | None,
) -> list[str]:
    normalized = normalize_size_rotation_feature(
        config, default_enabled=False,
    )
    return list(normalized["feature_names"]) if normalized["enabled"] else []


def size_rotation_lookback_sessions(
    config: Mapping[str, Any] | None,
) -> int:
    normalized = normalize_size_rotation_feature(
        config, default_enabled=False,
    )
    if not normalized["enabled"]:
        return 1
    return int(normalized["return_window"]) + int(normalized["regime_window"])


def append_size_rotation_features(
    observations: pd.DataFrame,
    daily: pd.DataFrame,
    large_membership: pd.DataFrame,
    small_membership: pd.DataFrame,
    config: Mapping[str, Any] | None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Append point-in-time size regime interactions to stock-date rows."""
    normalized = normalize_size_rotation_feature(
        config, default_enabled=False,
    )
    names = size_rotation_feature_names(normalized)
    if not normalized["enabled"]:
        return observations.copy(), {
            "feature_names": [],
            "coverage": {},
            "signal_date_count": 0,
        }

    observed = _observations(observations)
    prices = _daily_values(daily)
    large = _membership(large_membership, "大盘池")
    small = _membership(small_membership, "小盘池")
    return_window = int(normalized["return_window"])
    basket_size = int(normalized["basket_size"])
    regime_window = int(normalized["regime_window"])

    prices["stock_return"] = prices.groupby(
        "instrument", sort=False,
    )["adjusted_close"].pct_change(periods=return_window, fill_method=None)
    large_leg = _basket_return(
        prices, large, basket_size=basket_size, largest=True,
    ).rename("large_return")
    small_leg = _basket_return(
        prices, small, basket_size=basket_size, largest=False,
    ).rename("small_return")
    regime = pd.concat([large_leg, small_leg], axis=1).sort_index()
    regime["rotation_spread"] = (
        regime["small_return"] - regime["large_return"]
    )
    regime["rotation_z"] = _rolling_zscore(
        regime["rotation_spread"], regime_window,
    )
    regime["large_return_z"] = _rolling_zscore(
        regime["large_return"], regime_window,
    )

    merged = observed.merge(
        prices[[
            "trade_date", "instrument", "float_market_cap", "stock_return",
        ]],
        on=["trade_date", "instrument"],
        how="left",
        validate="one_to_one",
    )
    merged["_size_raw"] = -np.log(merged["float_market_cap"])
    merged["_size_z"] = merged.groupby(
        "trade_date", sort=False,
    )["_size_raw"].transform(_cross_sectional_zscore)
    merged = merged.merge(
        regime[["rotation_z", "large_return_z"]],
        left_on="trade_date",
        right_index=True,
        how="left",
        validate="many_to_one",
    )
    merged[names[0]] = merged["_size_raw"]
    merged[names[1]] = merged["_size_z"] * merged["stock_return"]
    merged[names[2]] = merged["_size_z"] * merged["rotation_z"]
    merged[names[3]] = merged["_size_z"] * merged["large_return_z"]
    merged.drop(columns=[
        "float_market_cap", "stock_return", "_size_raw", "_size_z",
        "rotation_z", "large_return_z",
    ], inplace=True)
    coverage = {
        name: float(pd.to_numeric(merged[name], errors="coerce").notna().mean())
        if len(merged) else 0.0
        for name in names
    }
    valid_signals = regime[["rotation_z", "large_return_z"]].notna().all(axis=1)
    return merged, {
        "feature_names": names,
        "coverage": coverage,
        "signal_date_count": int(valid_signals.sum()),
        "large_pool": normalized["large_pool"],
        "small_pool": normalized["small_pool"],
        "return_window": return_window,
        "basket_size": basket_size,
        "regime_window": regime_window,
    }


def _normalize_pool(source: Any, *, enabled: bool) -> dict[str, Any] | None:
    if source is None and not enabled:
        return None
    if not isinstance(source, Mapping):
        raise ValueError("启用大小盘轮动前必须选择设置中心的冻结股票池")
    raw = dict(source)
    selector = raw.get("selector")
    if not isinstance(selector, Mapping):
        raise ValueError("大小盘轮动股票池缺少冻结selector")
    expected = {
        "schema_version": CONFIGURED_STOCK_POOL_SOURCE_SCHEMA_VERSION,
        "source_id": str(raw.get("source_id") or "").strip(),
        "source_kind": "configured_stock_pool",
        "label": str(raw.get("label") or "").strip(),
        "version": int(raw.get("version") or 0),
        "available": raw.get("available") is True,
        "pit": raw.get("pit") is True,
        "settings_revision": int(raw.get("settings_revision") or 0),
        "binding_id": str(raw.get("binding_id") or "").strip(),
        "binding_fingerprint": str(raw.get("binding_fingerprint") or "").strip(),
        "selector": {
            "field_role": str(selector.get("field_role") or "").strip(),
            "operator": str(selector.get("operator") or "").strip(),
            "value": str(selector.get("value") or "").strip(),
        },
        "benchmark_code": str(raw.get("benchmark_code") or "").strip(),
        "config_fingerprint": str(raw.get("config_fingerprint") or "").strip(),
    }
    required_strings = (
        "source_id", "label", "binding_id", "binding_fingerprint",
        "benchmark_code", "config_fingerprint",
    )
    if expected["schema_version"] != str(raw.get("schema_version") or ""):
        raise ValueError("大小盘轮动股票池schema_version不受支持")
    if str(raw.get("source_kind") or "") != "configured_stock_pool":
        raise ValueError("大小盘轮动只支持设置中心配置的股票池")
    if not all(expected[key] for key in required_strings):
        raise ValueError("大小盘轮动股票池冻结身份不完整")
    if expected["version"] < 1 or expected["settings_revision"] < 1:
        raise ValueError("大小盘轮动股票池版本无效")
    if not expected["available"] or not expected["pit"]:
        raise ValueError("大小盘轮动股票池必须可用且支持PIT")
    if expected["binding_id"] != "index_membership":
        raise ValueError("大小盘轮动股票池必须绑定指数成分节点")
    if expected["selector"] != {
        "field_role": "index_code",
        "operator": "eq",
        "value": expected["selector"]["value"],
    } or not expected["selector"]["value"]:
        raise ValueError("大小盘轮动股票池selector必须是index_code等值条件")
    return expected


def _observations(source: pd.DataFrame) -> pd.DataFrame:
    required = {"trade_date", "instrument"}
    if not required.issubset(source.columns):
        raise ValueError("大小盘轮动目标样本缺少trade_date或instrument")
    result = source.copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce")
    result["instrument"] = result["instrument"].astype(str)
    if result["trade_date"].isna().any():
        raise ValueError("大小盘轮动目标样本包含无效trade_date")
    if result.duplicated(["trade_date", "instrument"]).any():
        raise ValueError("大小盘轮动目标样本存在同日重复股票")
    return result


def _daily_values(source: pd.DataFrame) -> pd.DataFrame:
    required = {
        "trade_date", "instrument", "adjusted_close", "float_market_cap",
    }
    if not required.issubset(source.columns):
        raise ValueError("大小盘轮动行情缺少后复权收盘价或流通市值")
    result = source[list(required)].copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce")
    result["instrument"] = result["instrument"].astype(str)
    for column in ("adjusted_close", "float_market_cap"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=list(required))
    result = result.loc[
        (result["adjusted_close"] > 0) & (result["float_market_cap"] > 0)
    ]
    if result.duplicated(["trade_date", "instrument"]).any():
        raise ValueError("大小盘轮动行情存在同日重复股票")
    return result.sort_values(["instrument", "trade_date"], ignore_index=True)


def _membership(source: pd.DataFrame, label: str) -> pd.DataFrame:
    required = {"trade_date", "instrument"}
    if not required.issubset(source.columns):
        raise ValueError(f"{label}历史成员缺少trade_date或instrument")
    result = source[["trade_date", "instrument"]].copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce")
    result["instrument"] = result["instrument"].astype(str)
    result = result.dropna().drop_duplicates()
    return result


def _basket_return(
    daily: pd.DataFrame,
    membership: pd.DataFrame,
    *,
    basket_size: int,
    largest: bool,
) -> pd.Series:
    eligible = daily.merge(
        membership,
        on=["trade_date", "instrument"],
        how="inner",
        validate="one_to_one",
    )
    eligible = eligible.dropna(subset=["stock_return"])
    eligible.sort_values(
        ["trade_date", "float_market_cap", "instrument"],
        ascending=[True, not largest, True],
        inplace=True,
    )
    selected = eligible.groupby("trade_date", sort=True).head(basket_size)
    counts = selected.groupby("trade_date")["stock_return"].count()
    result = selected.groupby("trade_date")["stock_return"].mean()
    return result.where(counts >= basket_size)


def _rolling_zscore(values: pd.Series, window: int) -> pd.Series:
    rolling = values.rolling(window=window, min_periods=window)
    mean = rolling.mean()
    std = rolling.std(ddof=0)
    return (values - mean) / std.replace(0, np.nan)


def _cross_sectional_zscore(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    std = numeric.std(ddof=0)
    if not np.isfinite(std) or std == 0:
        return pd.Series(np.nan, index=values.index, dtype=float)
    return (numeric - numeric.mean()) / std


def _bounded_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label}必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label}必须在{minimum}至{maximum}之间")
    return value


__all__ = [
    "SIZE_ROTATION_FEATURE_NAMES",
    "SIZE_ROTATION_FEATURE_SCHEMA_VERSION",
    "append_size_rotation_features",
    "normalize_size_rotation_feature",
    "size_rotation_feature_names",
    "size_rotation_lookback_sessions",
]
