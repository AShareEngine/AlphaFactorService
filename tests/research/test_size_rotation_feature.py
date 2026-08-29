from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from factor_service.research.size_rotation_feature import (
    SIZE_ROTATION_FEATURE_NAMES,
    append_size_rotation_features,
    normalize_size_rotation_feature,
    size_rotation_lookback_sessions,
)


def _pool(source_id: str, index_code: str) -> dict:
    return {
        "schema_version": "alphablocks.configured-stock-pool-source.v1",
        "source_id": source_id,
        "source_kind": "configured_stock_pool",
        "label": source_id,
        "version": 9,
        "available": True,
        "pit": True,
        "settings_revision": 9,
        "binding_id": "index_membership",
        "binding_fingerprint": "a" * 64,
        "selector": {
            "field_role": "index_code",
            "operator": "eq",
            "value": index_code,
        },
        "benchmark_code": index_code,
        "config_fingerprint": ("b" if source_id == "large" else "c") * 64,
    }


def _config(**changes) -> dict:
    result = {
        "enabled": True,
        "large_pool": _pool("large", "LARGE.INDEX"),
        "small_pool": _pool("small", "SMALL.INDEX"),
        "return_window": 2,
        "basket_size": 5,
        "regime_window": 20,
    }
    result.update(changes)
    return normalize_size_rotation_feature(result, default_enabled=False)


def test_normalize_rejects_same_pool_and_reports_lookback() -> None:
    config = _config()

    assert size_rotation_lookback_sessions(config) == 22
    with pytest.raises(ValueError, match="不能相同"):
        _config(small_pool=_pool("large", "LARGE.INDEX"))


def test_size_rotation_uses_historical_baskets_and_varies_by_stock() -> None:
    dates = pd.date_range("2024-01-02", periods=24, freq="B")
    large_codes = [f"L{index}" for index in range(5)]
    small_codes = [f"S{index}" for index in range(5)]
    target_codes = ["T_BIG", "T_SMALL"]
    daily_rows = []
    for date_index, trade_date in enumerate(dates):
        for index, code in enumerate(large_codes):
            daily_rows.append({
                "trade_date": trade_date,
                "instrument": code,
                "adjusted_close": 100 + date_index * (2 + index / 10),
                "float_market_cap": 1_000_000 - index * 10_000,
            })
        for index, code in enumerate(small_codes):
            daily_rows.append({
                "trade_date": trade_date,
                "instrument": code,
                "adjusted_close": 100 + date_index * (1 + index / 20),
                "float_market_cap": 100_000 + index * 1_000,
            })
        daily_rows.extend([
            {
                "trade_date": trade_date,
                "instrument": "T_BIG",
                "adjusted_close": 100 + date_index,
                "float_market_cap": 900_000,
            },
            {
                "trade_date": trade_date,
                "instrument": "T_SMALL",
                "adjusted_close": 100 + date_index,
                "float_market_cap": 90_000,
            },
        ])
    daily = pd.DataFrame(daily_rows)
    observations = pd.DataFrame([
        {"trade_date": day, "instrument": code}
        for day in dates
        for code in target_codes
    ])
    large_membership = pd.DataFrame([
        {"trade_date": day, "instrument": code}
        for day in dates
        for code in large_codes
    ])
    small_membership = pd.DataFrame([
        {"trade_date": day, "instrument": code}
        for day in dates
        for code in small_codes
    ])

    result, details = append_size_rotation_features(
        observations,
        daily,
        large_membership,
        small_membership,
        _config(),
    )

    assert details["feature_names"] == list(SIZE_ROTATION_FEATURE_NAMES)
    assert details["signal_date_count"] == 3
    latest = result.loc[result["trade_date"] == dates[-1]].set_index("instrument")
    assert latest.loc["T_BIG", "size_float_style"] < latest.loc[
        "T_SMALL", "size_float_style"
    ]
    assert latest.loc[
        "T_BIG", "size_rotation_regime_interaction"
    ] == pytest.approx(-latest.loc[
        "T_SMALL", "size_rotation_regime_interaction"
    ])
    assert np.isfinite(latest[list(SIZE_ROTATION_FEATURE_NAMES)].to_numpy()).all()


def test_size_rotation_requires_complete_baskets() -> None:
    dates = pd.date_range("2024-01-02", periods=24, freq="B")
    daily = pd.DataFrame([
        {
            "trade_date": day,
            "instrument": code,
            "adjusted_close": 100 + day_index,
            "float_market_cap": 100_000 + code_index,
        }
        for day_index, day in enumerate(dates)
        for code_index, code in enumerate(["A", "B", "C", "D"])
    ])
    membership = daily[["trade_date", "instrument"]]
    observations = membership.loc[membership["instrument"].isin(["A", "B"])]

    result, details = append_size_rotation_features(
        observations, daily, membership, membership, _config(),
    )

    assert details["signal_date_count"] == 0
    assert result["size_rotation_regime_interaction"].isna().all()
