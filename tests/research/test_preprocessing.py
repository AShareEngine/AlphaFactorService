from __future__ import annotations

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from factor_service.research.preprocessing import (
    FEATURE_PREPROCESSING_SCHEMA_VERSION,
    normalize_feature_preprocessing,
    preprocess_feature_panel,
    preprocess_qlib_frame,
)


def _enabled_preprocessing() -> dict[str, object]:
    return normalize_feature_preprocessing({}, default_enabled=True)


def test_normalize_feature_preprocessing_returns_the_frozen_recipe() -> None:
    disabled = normalize_feature_preprocessing(None, default_enabled=False)
    enabled = normalize_feature_preprocessing(
        {"enabled": True},
        default_enabled=False,
    )

    assert disabled == {
        "schema_version": FEATURE_PREPROCESSING_SCHEMA_VERSION,
        "enabled": False,
        "missing": {
            "method": "cross_sectional_median",
            "all_missing_value": 0.0,
        },
        "winsorize": {
            "method": "quantile",
            "lower": 0.01,
            "upper": 0.99,
            "minimum_observations": 10,
        },
        "standardize": {
            "method": "zscore",
            "ddof": 0,
            "constant_value": 0.0,
        },
    }
    assert enabled == {**disabled, "enabled": True}
    assert normalize_feature_preprocessing(
        enabled,
        default_enabled=False,
    ) == enabled


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ({"enabled": 1}, "enabled必须是布尔值"),
        ({"schema_version": "future.v2"}, "schema_version不受支持"),
        ({"unknown": True}, "未支持字段"),
        (
            {"winsorize": {"method": "quantile", "lower": 0.05}},
            "只支持固定基础截面处理口径",
        ),
    ],
)
def test_normalize_feature_preprocessing_rejects_non_frozen_contracts(
    source: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_feature_preprocessing(source, default_enabled=False)


def test_enabled_preprocessing_fills_daily_median_and_uses_population_zscore() -> None:
    frame = pd.DataFrame({
        "trade_date": pd.to_datetime([
            "2024-01-02", "2024-01-02", "2024-01-02",
            "2024-01-03", "2024-01-03", "2024-01-03",
        ]),
        "alpha": [1.0, 3.0, np.nan, 10.0, 30.0, np.nan],
    })

    result = preprocess_feature_panel(
        frame,
        ["alpha"],
        _enabled_preprocessing(),
    )

    expected_section = np.array([-np.sqrt(1.5), np.sqrt(1.5), 0.0])
    np.testing.assert_allclose(
        result["alpha"].to_numpy(),
        np.tile(expected_section, 2),
    )
    daily = result.groupby("trade_date")["alpha"]
    np.testing.assert_allclose(daily.mean().to_numpy(), [0.0, 0.0], atol=1e-15)
    np.testing.assert_allclose(
        daily.std(ddof=0).to_numpy(),
        [1.0, 1.0],
    )


def test_enabled_preprocessing_winsorizes_one_and_ninety_nine_percent_at_ten_rows() -> None:
    raw = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 1000.0])
    frame = pd.DataFrame({
        "trade_date": pd.Timestamp("2024-01-02"),
        "alpha": raw,
    })

    result = preprocess_feature_panel(
        frame,
        ["alpha"],
        _enabled_preprocessing(),
    )["alpha"].to_numpy()

    clipped = np.clip(raw, np.quantile(raw, 0.01), np.quantile(raw, 0.99))
    expected = (clipped - clipped.mean()) / clipped.std(ddof=0)
    without_winsorization = (raw - raw.mean()) / raw.std(ddof=0)
    np.testing.assert_allclose(result, expected)
    assert not np.allclose(result, without_winsorization)
    assert result.mean() == pytest.approx(0.0, abs=1e-15)
    assert result.std(ddof=0) == pytest.approx(1.0)


def test_enabled_preprocessing_handles_degenerate_and_non_finite_sections() -> None:
    frame = pd.DataFrame({
        "trade_date": pd.to_datetime([
            "2024-01-02", "2024-01-02", "2024-01-02",
            "2024-01-03",
            "2024-01-04", "2024-01-04",
            "2024-01-05", "2024-01-05", "2024-01-05",
        ]),
        "alpha": [
            7.0, 7.0, 7.0,
            42.0,
            np.nan, np.nan,
            1.0, np.inf, -np.inf,
        ],
    })

    result = preprocess_feature_panel(
        frame,
        ["alpha"],
        _enabled_preprocessing(),
    )

    assert np.isfinite(result["alpha"].to_numpy()).all()
    np.testing.assert_array_equal(result["alpha"].to_numpy(), np.zeros(len(frame)))


def test_enabled_preprocessing_isolates_each_date_from_future_observations() -> None:
    first_date = pd.Timestamp("2024-01-02")
    second_date = pd.Timestamp("2024-01-03")
    frame = pd.DataFrame({
        "trade_date": [first_date] * 10 + [second_date] * 10,
        "alpha": np.r_[np.arange(10, dtype=float), np.arange(10, 20, dtype=float)],
    })
    changed_future = frame.copy()
    changed_future.loc[changed_future["trade_date"] == second_date, "alpha"] = [
        -np.inf, np.nan, 1e12, -1e12, 80.0, 90.0, 100.0, 110.0, 120.0, np.inf,
    ]

    original = preprocess_feature_panel(
        frame,
        ["alpha"],
        _enabled_preprocessing(),
    )
    mutated = preprocess_feature_panel(
        changed_future,
        ["alpha"],
        _enabled_preprocessing(),
    )

    original_past = original.loc[original["trade_date"] == first_date, "alpha"]
    mutated_past = mutated.loc[mutated["trade_date"] == first_date, "alpha"]
    np.testing.assert_array_equal(original_past.to_numpy(), mutated_past.to_numpy())


def test_disabled_preprocessing_only_uses_finite_train_fallbacks() -> None:
    frame = pd.DataFrame({
        "trade_date": pd.to_datetime([
            "2024-01-02", "2024-01-02", "2024-01-03",
        ]),
        "alpha": [1.0, np.nan, np.inf],
        "beta": [np.nan, 9.0, -np.inf],
    })

    result = preprocess_feature_panel(
        frame,
        ["alpha", "beta"],
        {"enabled": False},
        fallback_values={"alpha": 7.5, "beta": np.inf},
    )

    np.testing.assert_array_equal(result["alpha"].to_numpy(), [1.0, 7.5, 7.5])
    np.testing.assert_array_equal(result["beta"].to_numpy(), [0.0, 9.0, 0.0])
    pdt.assert_series_equal(result["trade_date"], frame["trade_date"])


def test_enabled_preprocessing_keeps_excluded_features_unscaled() -> None:
    frame = pd.DataFrame({
        "trade_date": pd.to_datetime([
            "2024-01-02", "2024-01-02", "2024-01-03",
        ]),
        "is_special": [0.0, 1.0, np.nan],
    })

    result = preprocess_feature_panel(
        frame,
        ["is_special"],
        _enabled_preprocessing(),
        fallback_values={"is_special": 0.0},
        excluded_features=["is_special"],
    )

    np.testing.assert_array_equal(result["is_special"].to_numpy(), [0.0, 1.0, 0.0])


def test_qlib_bridge_preserves_index_labels_other_columns_and_input() -> None:
    index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2024-01-02"), "A"),
            (pd.Timestamp("2024-01-02"), "B"),
            (pd.Timestamp("2024-01-03"), "A"),
            (pd.Timestamp("2024-01-03"), "B"),
        ],
        names=["datetime", "instrument"],
    )
    columns = pd.MultiIndex.from_tuples([
        ("feature", "alpha"),
        ("label", "LABEL0"),
        ("meta", "raw_value"),
    ])
    raw = pd.DataFrame(
        [
            [1.0, 0.1, 101.0],
            [3.0, 0.2, 102.0],
            [10.0, 0.3, 103.0],
            [30.0, 0.4, 104.0],
        ],
        index=index,
        columns=columns,
    )
    untouched = raw.copy(deep=True)

    result = preprocess_qlib_frame(
        raw,
        ["alpha"],
        _enabled_preprocessing(),
    )

    pdt.assert_index_equal(result.index, raw.index, exact=True)
    pdt.assert_index_equal(result.columns, raw.columns, exact=True)
    pdt.assert_series_equal(result[("label", "LABEL0")], raw[("label", "LABEL0")])
    pdt.assert_series_equal(result[("meta", "raw_value")], raw[("meta", "raw_value")])
    np.testing.assert_allclose(
        result[("feature", "alpha")].to_numpy(),
        [-1.0, 1.0, -1.0, 1.0],
    )
    pdt.assert_frame_equal(raw, untouched)

