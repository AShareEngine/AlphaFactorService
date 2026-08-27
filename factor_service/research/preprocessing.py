from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd


FEATURE_PREPROCESSING_SCHEMA_VERSION = (
    "alphablocks.cross-sectional-feature-preprocessing.v1"
)
DATASET_PIPELINE_VERSION = "alphablocks.dataset-pipeline.v8"
LEGACY_DATASET_PIPELINE_VERSIONS = frozenset({
    "alphablocks.dataset-pipeline.v1",
    "alphablocks.dataset-pipeline.v2",
    "alphablocks.dataset-pipeline.v3",
    "alphablocks.dataset-pipeline.v4",
    "alphablocks.dataset-pipeline.v5",
    "alphablocks.dataset-pipeline.v6",
    "alphablocks.dataset-pipeline.v7",
})
_DEFAULT_QUANTILES = (0.01, 0.99)
_DEFAULT_MINIMUM_OBSERVATIONS = 10
_EPSILON = 1e-12


def normalize_feature_preprocessing(
    source: Mapping[str, Any] | None,
    *,
    default_enabled: bool,
) -> dict[str, Any]:
    """Return the single supported immutable feature-preprocessing contract."""
    raw = dict(source or {})
    unknown = sorted(
        set(raw)
        - {"schema_version", "enabled", "missing", "winsorize", "standardize"}
    )
    if unknown:
        raise ValueError(
            "preprocessing包含未支持字段: " + ", ".join(unknown)
        )
    enabled = raw.get("enabled", default_enabled)
    if not isinstance(enabled, bool):
        raise ValueError("preprocessing.enabled必须是布尔值")

    expected = {
        "schema_version": FEATURE_PREPROCESSING_SCHEMA_VERSION,
        "enabled": enabled,
        "missing": {
            "method": "cross_sectional_median",
            "all_missing_value": 0.0,
        },
        "winsorize": {
            "method": "quantile",
            "lower": _DEFAULT_QUANTILES[0],
            "upper": _DEFAULT_QUANTILES[1],
            "minimum_observations": _DEFAULT_MINIMUM_OBSERVATIONS,
        },
        "standardize": {
            "method": "zscore",
            "ddof": 0,
            "constant_value": 0.0,
        },
    }
    schema_version = raw.get("schema_version")
    if schema_version not in {None, FEATURE_PREPROCESSING_SCHEMA_VERSION}:
        raise ValueError("preprocessing.schema_version不受支持")
    for section in ("missing", "winsorize", "standardize"):
        configured = raw.get(section)
        if configured is not None and configured != expected[section]:
            raise ValueError(f"preprocessing.{section}当前只支持固定基础截面处理口径")
    return expected


def preprocess_feature_panel(
    frame: pd.DataFrame,
    feature_names: Sequence[str],
    preprocessing: Mapping[str, Any] | None,
    *,
    fallback_values: Mapping[str, float] | None = None,
    excluded_features: Sequence[str] = (),
) -> pd.DataFrame:
    """Apply the frozen daily cross-sectional recipe to a regular feature panel.

    ``frame`` must contain ``trade_date`` plus one column for every feature.
    Categorical/boolean columns listed in ``excluded_features`` retain their
    values and use the train-only fallback value solely for missing rows.
    """
    names = [str(name) for name in feature_names]
    missing_columns = [name for name in names if name not in frame.columns]
    if missing_columns:
        raise ValueError("特征预处理缺少列: " + ", ".join(missing_columns))
    if "trade_date" not in frame.columns:
        raise ValueError("特征预处理缺少trade_date")

    config = normalize_feature_preprocessing(
        preprocessing,
        default_enabled=False,
    )
    fallbacks = dict(fallback_values or {})
    excluded = {str(name) for name in excluded_features}
    out = frame.copy()
    trade_dates = pd.to_datetime(out["trade_date"], errors="coerce")
    if trade_dates.isna().any():
        raise ValueError("特征预处理包含无效trade_date")

    for name in names:
        numeric = pd.to_numeric(out[name], errors="coerce").astype(float)
        out[name] = numeric.where(np.isfinite(numeric), np.nan)

    if not config["enabled"]:
        for name in names:
            out[name] = out[name].fillna(_finite_fallback(fallbacks.get(name)))
        return out

    date_positions = _date_positions(trade_dates)
    for name in names:
        if name in excluded:
            out[name] = out[name].fillna(_finite_fallback(fallbacks.get(name)))
            continue
        source_values = out[name].to_numpy(dtype=np.float64)
        transformed = np.empty(len(out), dtype=np.float64)
        transformed.fill(np.nan)
        for positions in date_positions:
            transformed[positions] = _preprocess_cross_section(
                source_values[positions], config,
            )
        out[name] = transformed

    if not np.isfinite(out[names].to_numpy(dtype=np.float64)).all():
        raise ValueError("特征截面预处理后仍包含非有限值")
    return out


def preprocess_qlib_frame(
    raw_frame: pd.DataFrame,
    feature_names: Sequence[str],
    preprocessing: Mapping[str, Any] | None,
    *,
    fallback_values: Mapping[str, float] | None = None,
    excluded_features: Sequence[str] = (),
) -> pd.DataFrame:
    """Apply ``preprocess_feature_panel`` without changing a Qlib frame shape."""
    if "datetime" not in raw_frame.index.names:
        raise ValueError("Qlib特征帧索引缺少datetime")
    names = [str(name) for name in feature_names]
    panel = pd.DataFrame({
        "trade_date": pd.to_datetime(
            raw_frame.index.get_level_values("datetime"), errors="coerce",
        )
    })
    for name in names:
        key = ("feature", name)
        if key not in raw_frame.columns:
            raise ValueError(f"Qlib特征帧缺少{name}")
        panel[name] = raw_frame[key].to_numpy(copy=True)
    processed = preprocess_feature_panel(
        panel,
        names,
        preprocessing,
        fallback_values=fallback_values,
        excluded_features=excluded_features,
    )
    result = raw_frame.copy()
    for name in names:
        result[("feature", name)] = processed[name].to_numpy(dtype=np.float64)
    return result


def _date_positions(trade_dates: pd.Series) -> list[np.ndarray]:
    positions = pd.Series(
        np.arange(len(trade_dates), dtype=np.int64),
        index=trade_dates.to_numpy(),
    )
    return [
        group.to_numpy(dtype=np.int64)
        for _date, group in positions.groupby(level=0, sort=True)
    ]


def _preprocess_cross_section(
    values: np.ndarray,
    config: Mapping[str, Any],
) -> np.ndarray:
    result = np.asarray(values, dtype=np.float64).copy()
    valid = np.isfinite(result)
    if valid.any():
        median = float(np.median(result[valid]))
        result[~valid] = median
    else:
        result.fill(float(config["missing"]["all_missing_value"]))

    winsor = config["winsorize"]
    if len(result) >= int(winsor["minimum_observations"]):
        lower = float(np.quantile(result, float(winsor["lower"])))
        upper = float(np.quantile(result, float(winsor["upper"])))
        if np.isfinite(lower) and np.isfinite(upper) and lower < upper:
            result = np.clip(result, lower, upper)

    mean = float(np.mean(result))
    std = float(np.std(result, ddof=int(config["standardize"]["ddof"])))
    if not np.isfinite(std) or std <= _EPSILON:
        result.fill(float(config["standardize"]["constant_value"]))
    else:
        result = (result - mean) / std
    return result


def _finite_fallback(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if np.isfinite(number) else 0.0


__all__ = [
    "DATASET_PIPELINE_VERSION",
    "FEATURE_PREPROCESSING_SCHEMA_VERSION",
    "LEGACY_DATASET_PIPELINE_VERSIONS",
    "normalize_feature_preprocessing",
    "preprocess_feature_panel",
    "preprocess_qlib_frame",
]
