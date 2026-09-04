from __future__ import annotations

from contextlib import nullcontext
import pandas as pd
import pytest
from types import SimpleNamespace

from factor_service.research import dataset as dataset_module
from factor_service.research.dataset import (
    DatasetBuilder,
    _assemble_factor_feature_matrix,
    _future_direction_label,
    _future_rank_label,
    _industry_features,
    _industry_rank_label,
    materialized_dataset_segments,
    resolve_dataset_split,
    split_trading_dates,
    split_trading_dates_by_dates,
    walk_forward_segments,
)
from factor_service.research.errors import JobCanceled
from factor_service.research.industry_feature import (
    industry_feature_names,
    normalize_industry_feature,
)
from factor_service.research.size_rotation_feature import (
    normalize_size_rotation_feature,
)
from factor_service.research.job import CancellationToken
from factor_service.entity_field_feature import normalize_entity_field_feature
from tests.research.utils import valid_job


def _configured_rotation_pool(source_id: str, index_code: str) -> dict:
    return {
        "schema_version": "alphablocks.configured-stock-pool-source.v1",
        "source_id": source_id,
        "source_kind": "configured_stock_pool",
        "label": source_id,
        "version": 1,
        "available": True,
        "pit": True,
        "settings_revision": 1,
        "binding_id": "index_membership",
        "binding_fingerprint": "a" * 64,
        "selector": {
            "field_role": "index_code",
            "operator": "eq",
            "value": index_code,
        },
        "benchmark_code": index_code,
        "config_fingerprint": source_id[0] * 64,
    }


def test_size_rotation_lookback_uses_first_observed_trading_date() -> None:
    builder = DatasetBuilder.__new__(DatasetBuilder)
    observed = pd.DataFrame([{
        "trade_date": pd.Timestamp("2025-02-05"),
        "instrument": "A",
    }])
    calls = []
    builder.trading_dates_ending_at = lambda value, count, **_kwargs: (
        calls.append((value, count)) or ["2024-10-22"]
    )
    builder._membership = lambda *_args, **_kwargs: pd.DataFrame([{
        "trade_date": pd.Timestamp("2025-02-05"),
        "instrument": "A",
    }])
    config = normalize_size_rotation_feature({
        "enabled": True,
        "large_pool": _configured_rotation_pool("large", "LARGE.INDEX"),
        "small_pool": _configured_rotation_pool("small", "SMALL.INDEX"),
        "return_window": 10,
        "basket_size": 20,
        "regime_window": 60,
    }, default_enabled=False)

    with pytest.raises(ValueError, match="冻结的训练基础行情绑定"):
        builder._size_rotation_features(
            observed,
            date_start="2025-02-03",
            date_end="2025-02-05",
            index_code="000905.SH",
            universe_id="csi500",
            size_rotation_feature=config,
            data_bindings=None,
            data_cutoff="2026-08-30T00:00:00+08:00",
        )

    assert calls == [("2025-02-05", 70)]


def test_future_five_day_label_is_cross_sectional_and_centered() -> None:
    dates = pd.date_range("2024-01-02", periods=7, freq="B")
    prices = pd.DataFrame([
        {"trade_date": day, "instrument": code, "adjusted_close": 10 + day_index * growth}
        for day_index, day in enumerate(dates)
        for code, growth in (("A", 1.0), ("B", 0.1))
    ])

    labels = _future_rank_label(prices, horizon=5)
    first = labels[labels["trade_date"] == dates[0]].set_index("instrument")["LABEL0"]

    assert first["A"] == pytest.approx(1.0)
    assert first["B"] == pytest.approx(0.0)


def test_future_direction_label_matches_quantmind_binary_definition() -> None:
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    prices = pd.DataFrame([
        {"trade_date": day, "instrument": code, "adjusted_close": value}
        for day, rows in (
            (dates[0], (("UP", 10.0), ("DOWN", 10.0), ("FLAT", 10.0))),
            (dates[1], (("UP", 11.0), ("DOWN", 9.0), ("FLAT", 10.0))),
            (dates[2], (("UP", 12.0), ("DOWN", 8.0), ("FLAT", 10.0))),
        )
        for code, value in rows
    ])

    labels = _future_direction_label(prices, horizon=1)
    first = labels[labels["trade_date"] == dates[0]].set_index("instrument")["LABEL0"]

    assert first.to_dict() == {"DOWN": 0.0, "FLAT": 0.0, "UP": 1.0}
    assert labels["LABEL0"].notna().all()
    assert dates[-1] not in set(labels["trade_date"])


def test_industry_features_use_signal_day_weights() -> None:
    trade_date = pd.Timestamp("2024-01-02")
    features = pd.DataFrame([
        {"trade_date": trade_date, "instrument": "A", "factor": 1.0},
        {"trade_date": trade_date, "instrument": "B", "factor": 3.0},
        {"trade_date": trade_date, "instrument": "C", "factor": 8.0},
    ])
    membership = pd.DataFrame([
        {
            "trade_date": trade_date, "instrument": "A",
            "industry_entity": "801010.SI", "industry_name": "农林牧渔",
            "industry_weight": 25.0,
        },
        {
            "trade_date": trade_date, "instrument": "B",
            "industry_entity": "801010.SI", "industry_name": "农林牧渔",
            "industry_weight": 75.0,
        },
        {
            "trade_date": trade_date, "instrument": "C",
            "industry_entity": "801020.SI", "industry_name": "煤炭",
            "industry_weight": 100.0,
        },
    ])

    aggregated, frozen_membership = _industry_features(
        features, membership, ["factor"],
    )

    values = aggregated.set_index("instrument")["factor"]
    assert values["801010.SI"] == pytest.approx(2.5)
    assert values["801020.SI"] == pytest.approx(8.0)
    assert len(frozen_membership) == 3


def test_industry_label_ranks_weighted_future_industry_returns() -> None:
    dates = pd.date_range("2024-01-02", periods=2, freq="B")
    prices = pd.DataFrame([
        {"trade_date": dates[0], "instrument": code, "adjusted_close": 10.0}
        for code in ("A", "B", "C")
    ] + [
        {"trade_date": dates[1], "instrument": code, "adjusted_close": value}
        for code, value in (("A", 11.0), ("B", 13.0), ("C", 9.0))
    ])
    membership = pd.DataFrame([
        {
            "trade_date": dates[0], "instrument": code,
            "industry_entity": industry, "industry_name": industry,
            "industry_weight": weight,
        }
        for code, industry, weight in (
            ("A", "801010.SI", 25.0),
            ("B", "801010.SI", 75.0),
            ("C", "801020.SI", 100.0),
        )
    ])

    labels = _industry_rank_label(prices, membership, horizon=1)
    values = labels.set_index("instrument")["LABEL0"]

    assert values["801010.SI"] == pytest.approx(1.0)
    assert values["801020.SI"] == pytest.approx(-1.0)


def test_industry_membership_rejects_pre_sw2021_cutover_without_query() -> None:
    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace(source_database="starlight")
    builder.client = SimpleNamespace(
        query=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pre-cutover query must not run")
        ),
    )
    observations = pd.DataFrame([{
        "trade_date": pd.Timestamp("2021-12-10"), "instrument": "A",
    }])

    with pytest.raises(ValueError, match="2021-12-13"):
        builder._industry_membership(
            observations, "2021-12-10", "2021-12-10",
        )


def test_industry_membership_rejects_duplicate_signal_day_assignment() -> None:
    class _Client:
        @staticmethod
        def query(_query, parameters):
            assert parameters["date_start"] == "2024-01-02"
            return SimpleNamespace(result_rows=[
                (pd.Timestamp("2024-01-02"), "A", "801010.SI", "农林牧渔", 50.0),
                (pd.Timestamp("2024-01-02"), "A", "801020.SI", "煤炭", 50.0),
            ])

    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace(source_database="starlight")
    builder.client = _Client()
    observations = pd.DataFrame([{
        "trade_date": pd.Timestamp("2024-01-02"), "instrument": "A",
    }])

    with pytest.raises(ValueError, match="重复归属"):
        builder._industry_membership(
            observations, "2024-01-02", "2024-01-02",
        )


def test_dataset_build_appends_pit_industry_one_hot_and_excludes_it_from_scaling(
    monkeypatch,
) -> None:
    dates = pd.date_range("2024-01-02", periods=100, freq="B")
    instruments = [f"S{index:02d}" for index in range(10)]
    membership = pd.DataFrame([
        {"trade_date": day, "instrument": code}
        for day in dates
        for code in instruments
    ])
    factor_values = membership.copy()
    factor_values["value"] = [
        float(index) for index in range(len(factor_values))
    ]
    industry_membership = membership.copy()
    industry_membership["industry_entity"] = [
        (
            "801030.SI"
            if code == "S00" and day >= dates[50]
            else "801010.SI"
            if code < "S05"
            else "801030.SI"
        )
        for day, code in zip(
            industry_membership["trade_date"],
            industry_membership["instrument"],
        )
    ]
    industry_membership["industry_name"] = "行业"
    industry_membership["industry_weight"] = 1.0

    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace()
    builder._membership = lambda *_args, **_kwargs: membership.copy()
    builder._factor_definition = lambda _item: SimpleNamespace(output_type="number")
    builder._factor_values = lambda *_args, **_kwargs: factor_values.copy()
    builder._adjusted_close = lambda *_args, **_kwargs: pd.DataFrame()
    industry_state = {"frame": industry_membership}
    builder._industry_membership = (
        lambda *_args, **_kwargs: industry_state["frame"].copy()
    )

    def fake_labels(_prices, *, horizon):
        assert horizon == 5
        labels = membership.copy()
        labels["LABEL0"] = 0.0
        return labels

    monkeypatch.setattr(dataset_module, "_future_rank_label", fake_labels)
    contract = normalize_industry_feature(
        {"enabled": True}, default_enabled=False,
    )
    one_hot_names = industry_feature_names(contract)
    job = valid_job()
    job["dataset_spec"].update({
        "date_start": dates[0].date().isoformat(),
        "date_end": dates[-1].date().isoformat(),
        "research_target": "stock_selection",
        "target_mode": "return",
        "preprocessing": {"enabled": True},
        "industry_feature": contract,
        "target_ref": {"id": "stock_selection", "version": 1},
        "transform_refs": [{
            "id": "sw2021_industry_one_hot", "version": 2,
        }],
        "universe_rule_refs": [{"id": "exclude_st", "version": 1}],
    })

    prepared = builder.build(job)

    assert prepared.feature_names[-32:] == one_hot_names
    assert prepared.manifest["industry_feature"] == contract
    assert prepared.manifest["target_ref"] == {
        "id": "stock_selection", "version": 1,
    }
    assert prepared.manifest["transform_refs"][0]["version"] == 2
    assert prepared.manifest["universe_rule_refs"][0]["id"] == "exclude_st"
    assert prepared.manifest["industry_feature_details"] == {
        "feature_names": one_hot_names,
        "mapped_coverage": pytest.approx(1.0),
        "unknown_rows": 0,
        "category_count": 31,
    }
    assert set(one_hot_names).issubset(
        prepared.manifest["preprocessing_excluded_features"]
    )
    one_hot_frame = prepared.frame.loc[:, [
        ("feature", name) for name in one_hot_names
    ]]
    assert (one_hot_frame.sum(axis=1) == 1.0).all()
    first_code = "industry_sw2021_l1__801010_si"
    second_code = "industry_sw2021_l1__801030_si"
    assert prepared.raw_frame.loc[
        (dates[0], "S00"), ("feature", first_code)
    ] == 1.0
    assert prepared.raw_frame.loc[
        (dates[-1], "S00"), ("feature", second_code)
    ] == 1.0
    assert prepared.frame.loc[
        (dates[0], "S00"), ("feature", first_code)
    ] == 1.0
    assert prepared.frame.loc[
        (dates[-1], "S00"), ("feature", second_code)
    ] == 1.0

    # Training and inference share dataset.minimum_factor_coverage.  A dataset
    # accepted at the training stage must not become unscorable solely because
    # inference applies a stricter industry threshold.
    partly_unknown = industry_membership.copy()
    partly_unknown.loc[
        partly_unknown.index[:100], "industry_entity"
    ] = "899999.SI"
    industry_state["frame"] = partly_unknown
    job["dataset_spec"]["minimum_factor_coverage"] = 0.95

    with pytest.raises(ValueError, match=r"90\.00%低于95%"):
        builder.build(job)


def test_split_has_five_session_embargo_between_segments() -> None:
    dates = pd.date_range("2024-01-02", periods=100, freq="B")
    segments = split_trading_dates(pd.Index(dates), embargo_days=5)

    train_end = dates.get_loc(pd.Timestamp(segments["train"][1]))
    valid_start = dates.get_loc(pd.Timestamp(segments["valid"][0]))
    valid_end = dates.get_loc(pd.Timestamp(segments["valid"][1]))
    test_start = dates.get_loc(pd.Timestamp(segments["test"][0]))
    assert valid_start - train_end == 6
    assert test_start - valid_end == 6


def test_materialization_reuses_frozen_ratio_boundaries_when_samples_miss_day() -> None:
    calendar = pd.date_range("2024-01-02", periods=105, freq="B")
    split = {
        "mode": "ratio", "train": 0.6, "valid": 0.2, "test": 0.2,
        "embargo_days": 5,
    }
    resolution = resolve_dataset_split(
        calendar, split=split, label_horizon_trading_days=5,
    )
    frozen = {**split, "resolved": resolution}
    sparse_samples = calendar[:-5].delete(58)

    segments = materialized_dataset_segments(
        split=frozen,
        membership_calendar=calendar,
        available_sample_dates=sparse_samples,
        label_horizon_trading_days=5,
    )

    assert segments == {
        name: tuple(value)
        for name, value in resolution["segments"].items()
    }
    assert segments != split_trading_dates(
        sparse_samples,
        embargo_days=5,
        train_ratio=0.6,
        valid_ratio=0.2,
    )


def test_materialization_rejects_frozen_calendar_drift() -> None:
    calendar = pd.date_range("2024-01-02", periods=105, freq="B")
    split = {
        "mode": "ratio", "train": 0.6, "valid": 0.2, "test": 0.2,
        "embargo_days": 5,
    }
    frozen = {
        **split,
        "resolved": resolve_dataset_split(
            calendar, split=split, label_horizon_trading_days=5,
        ),
    }

    with pytest.raises(ValueError, match="交易日历或切分边界已漂移"):
        materialized_dataset_segments(
            split=frozen,
            membership_calendar=calendar.delete(20),
            available_sample_dates=calendar[:-5],
            label_horizon_trading_days=5,
        )


def test_split_rejects_too_little_history() -> None:
    with pytest.raises(ValueError, match="不足60天"):
        split_trading_dates(pd.Index(pd.date_range("2024-01-02", periods=30, freq="B")))


def test_split_honors_custom_valid_and_test_ratios() -> None:
    dates = pd.date_range("2024-01-02", periods=200, freq="B")
    segments = split_trading_dates(
        pd.Index(dates), embargo_days=5,
        train_ratio=0.7, valid_ratio=0.15,
    )
    train_end = dates.get_loc(pd.Timestamp(segments["train"][1]))
    valid_start = dates.get_loc(pd.Timestamp(segments["valid"][0]))
    valid_end = dates.get_loc(pd.Timestamp(segments["valid"][1]))
    test_start = dates.get_loc(pd.Timestamp(segments["test"][0]))
    # 200 * 0.7 = 140，训练集约占前140个交易日（含末尾5日隔离）
    assert valid_start == 140
    # 200 * 0.85 = 170，测试集从第170个交易日开始
    assert test_start == 170
    assert valid_start - train_end == 6
    assert test_start - valid_end == 6
    assert segments["test"][1] == dates[-1].date().isoformat()


def test_split_rejects_invalid_ratios() -> None:
    with pytest.raises(ValueError, match="不得低于5%"):
        split_trading_dates(
            pd.Index(pd.date_range("2024-01-02", periods=200, freq="B")),
            train_ratio=0.98, valid_ratio=0.01,
        )
    with pytest.raises(ValueError, match="必须是有效数字"):
        split_trading_dates(
            pd.Index(pd.date_range("2024-01-02", periods=200, freq="B")),
            train_ratio=0.6, valid_ratio=float("nan"),
        )


def test_explicit_date_split_requires_real_boundaries_and_embargo() -> None:
    dates = pd.date_range("2024-01-02", periods=100, freq="B")
    segments = split_trading_dates_by_dates(
        pd.Index(dates),
        train=(dates[0], dates[59]),
        valid=(dates[65], dates[79]),
        test=(dates[85], dates[-1]),
        embargo_days=5,
    )

    assert segments == {
        "train": (
            dates[0].date().isoformat(), dates[59].date().isoformat(),
        ),
        "valid": (
            dates[65].date().isoformat(), dates[79].date().isoformat(),
        ),
        "test": (
            dates[85].date().isoformat(), dates[-1].date().isoformat(),
        ),
    }


def test_explicit_date_split_rejects_hidden_gaps() -> None:
    dates = pd.date_range("2024-01-02", periods=100, freq="B")
    with pytest.raises(ValueError, match="恰好5个交易日"):
        split_trading_dates_by_dates(
            pd.Index(dates),
            train=(dates[0], dates[58]),
            valid=(dates[65], dates[79]),
            test=(dates[85], dates[-1]),
            embargo_days=5,
        )
def test_walk_forward_rolling_windows_use_embargo_and_do_not_overlap_tests() -> None:
    dates = pd.date_range("2018-01-02", periods=1600, freq="B")
    windows = walk_forward_segments(
        pd.Index(dates), train_sessions=252, valid_sessions=63,
        test_sessions=20, step_sessions=20, embargo_sessions=5,
        oos_date_start=dates[1000].date().isoformat(),
        oos_date_end=dates[1059].date().isoformat(),
    )

    assert len(windows) == 3
    assert windows[-1]["test"][1] == dates[1059].date().isoformat()
    for window in windows:
        train_start = dates.get_loc(pd.Timestamp(window["train"][0]))
        train_end = dates.get_loc(pd.Timestamp(window["train"][1]))
        valid_start = dates.get_loc(pd.Timestamp(window["valid"][0]))
        valid_end = dates.get_loc(pd.Timestamp(window["valid"][1]))
        test_start = dates.get_loc(pd.Timestamp(window["test"][0]))
        assert train_end - train_start + 1 == 252
        assert valid_start - train_end == 6
        assert test_start - valid_end == 6
    for previous, current in zip(windows, windows[1:]):
        assert pd.Timestamp(previous["test"][1]) < pd.Timestamp(current["test"][0])


def test_walk_forward_zero_validation_uses_single_train_test_embargo() -> None:
    dates = pd.date_range("2018-01-02", periods=1000, freq="B")
    windows = walk_forward_segments(
        pd.Index(dates), train_sessions=252, valid_sessions=0,
        test_sessions=20, step_sessions=20, embargo_sessions=5,
        oos_date_start=dates[500].date().isoformat(),
        oos_date_end=dates[539].date().isoformat(),
    )

    assert len(windows) == 2
    for window in windows:
        assert "valid" not in window
        train_end = dates.get_loc(pd.Timestamp(window["train"][1]))
        test_start = dates.get_loc(pd.Timestamp(window["test"][0]))
        assert test_start - train_end == 6


def test_walk_forward_expanding_windows_keep_original_train_start() -> None:
    dates = pd.date_range("2018-01-02", periods=1600, freq="B")
    windows = walk_forward_segments(
        pd.Index(dates), strategy="expanding", train_sessions=252,
        valid_sessions=63, test_sessions=20, step_sessions=20,
        oos_date_start=dates[1000].date().isoformat(),
        oos_date_end=dates[1059].date().isoformat(),
    )

    assert len(windows) == 3
    assert {window["train"][0] for window in windows} == {dates[0].date().isoformat()}
    assert windows[0]["train"][1] < windows[-1]["train"][1]
    assert windows[-1]["test"][1] == dates[1059].date().isoformat()


def test_walk_forward_clips_partial_last_window_on_frozen_calendar_end() -> None:
    dates = pd.date_range("2018-01-02", periods=1000, freq="B")
    windows = walk_forward_segments(
        pd.Index(dates), train_sessions=252, valid_sessions=63,
        test_sessions=20, step_sessions=20, embargo_sessions=5,
        oos_date_start=dates[-45].date().isoformat(),
        oos_date_end=dates[-1].date().isoformat(),
    )

    assert len(windows) == 3
    assert windows[-1]["test"] == (
        dates[-5].date().isoformat(), dates[-1].date().isoformat(),
    )


def test_walk_forward_rejects_overlapping_test_windows() -> None:
    dates = pd.date_range("2018-01-02", periods=1000, freq="B")
    with pytest.raises(ValueError, match="步长必须等于测试窗口"):
        walk_forward_segments(
            pd.Index(dates), train_sessions=252, valid_sessions=63,
            test_sessions=20, step_sessions=10,
            oos_date_start=dates[500].date().isoformat(),
            oos_date_end=dates[559].date().isoformat(),
        )


def test_dataset_build_checks_cancellation_before_clickhouse_query() -> None:
    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace()
    builder._membership = lambda *_args: (_ for _ in ()).throw(AssertionError("query must not run"))
    cancellation = CancellationToken()
    cancellation.cancel("stop")

    with pytest.raises(JobCanceled, match="stop"):
        builder.build(valid_job(), cancellation=cancellation)


def test_feature_cross_section_is_independent_of_future_label_availability(
    monkeypatch,
) -> None:
    dates = pd.date_range("2024-01-02", periods=100, freq="B")
    instruments = [f"S{index:02d}" for index in range(10)]
    membership = pd.DataFrame([
        {"trade_date": day, "instrument": code}
        for day in dates
        for code in instruments
    ])
    factor_values = membership.copy()
    factor_values["value"] = [float(index) for index in range(len(membership))]
    factor_values.loc[
        (factor_values["trade_date"] == dates[20])
        & (factor_values["instrument"] == instruments[0]),
        "value",
    ] = float("nan")
    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace()
    builder._membership = lambda *_args, **_kwargs: membership.copy()
    builder._factor_definition = lambda _item: SimpleNamespace(output_type="number")
    builder._factor_values = lambda *_args, **_kwargs: factor_values.copy()
    builder._adjusted_close = lambda *_args, **_kwargs: pd.DataFrame()
    missing_label = {"enabled": False}
    affected_date = dates[10]

    def fake_labels(_prices, *, horizon):
        assert horizon == 5
        labels = membership.copy()
        labels["LABEL0"] = 0.0
        if missing_label["enabled"]:
            labels = labels.loc[~(
                (labels["trade_date"] == affected_date)
                & (labels["instrument"] == instruments[-1])
            )]
        return labels

    monkeypatch.setattr(dataset_module, "_future_rank_label", fake_labels)
    job = valid_job()
    job["dataset_spec"].update({
        "date_start": dates[0].date().isoformat(),
        "date_end": dates[-1].date().isoformat(),
        "preprocessing": {"enabled": True},
        "target_mode": "return",
        "research_target": "stock_selection",
    })

    complete = builder.build(job)
    missing_label["enabled"] = True
    incomplete = builder.build(job)

    feature = complete.feature_names[0]
    common_index = (affected_date, instruments[0])
    assert incomplete.frame.loc[common_index, ("feature", feature)] == pytest.approx(
        complete.frame.loc[common_index, ("feature", feature)],
    )
    assert len(complete.frame) == len(incomplete.frame) + 1

    # Rebuilding an old hash must retain its original labeled-panel median
    # semantics, even though v6 fixes that historical limitation.
    legacy_job = valid_job()
    legacy_job["dataset_spec"].update({
        "pipeline_version": "alphablocks.dataset-pipeline.v5",
        "date_start": dates[0].date().isoformat(),
        "date_end": dates[-1].date().isoformat(),
        "target_mode": "return",
        "research_target": "stock_selection",
    })
    missing_label["enabled"] = False
    legacy_complete = builder.build(legacy_job)
    missing_label["enabled"] = True
    legacy_incomplete = builder.build(legacy_job)

    assert legacy_complete.manifest["preprocessing_compatibility"] == (
        "legacy_labeled_panel_train_medians"
    )
    assert legacy_complete.medians[feature] != legacy_incomplete.medians[feature]


def test_sample_filters_apply_listing_age_and_daily_stock_status() -> None:
    class _Client:
        def query(self, query, parameters):
            if "bs_stock_basic" in query:
                return SimpleNamespace(result_rows=[
                    ("A", "2024-01-02"),
                    ("B", "2020-01-02"),
                    ("C", "2020-01-02"),
                ])
            if "SELECT DISTINCT toDate(trade_time)" in query:
                assert parameters["calendar_code"] == "000001.SH"
                return SimpleNamespace(result_rows=[
                    (pd.Timestamp("2024-01-02"),),
                    (pd.Timestamp("2024-01-03"),),
                    (pd.Timestamp("2024-01-04"),),
                ])
            if "ad_history_stock_status" in query:
                return SimpleNamespace(result_rows=[
                    (pd.Timestamp("2024-01-04"), "B", 1, 0),
                    (pd.Timestamp("2024-01-04"), "C", 0, 1),
                ])
            raise AssertionError(query)

    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace(source_database="starlight")
    builder.client = _Client()
    membership = pd.DataFrame([
        {"trade_date": pd.Timestamp("2024-01-04"), "instrument": code}
        for code in ("A", "B", "C")
    ])

    filtered = builder._apply_sample_filters(
        membership,
        date_start="2024-01-04",
        date_end="2024-01-04",
        sample_filters={
            "minimum_listing_trading_days": 2,
            "exclude_st": True,
            "exclude_delisting": True,
        },
    )

    assert filtered.to_dict("records") == [{
        "trade_date": pd.Timestamp("2024-01-04"), "instrument": "A",
    }]


def test_sample_filters_apply_safe_custom_formula_with_historical_window() -> None:
    class _Client:
        query_text = ""
        query_params = {}

        def query(self, query, parameters):
            self.query_text = query
            self.query_params = parameters
            assert "FROM ab_factor.stock_daily_factor_source" in query
            assert "ROWS BETWEEN 19 PRECEDING AND CURRENT ROW" in query
            assert "formula_0 = 1" in query
            return SimpleNamespace(result_rows=[
                (pd.Timestamp("2024-01-04"), "A"),
            ])

    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace(
        source_database="starlight",
        factor_database="ab_factor",
        stock_daily_table="stock_daily_factor_source",
    )
    builder.client = _Client()
    membership = pd.DataFrame([
        {"trade_date": pd.Timestamp("2024-01-04"), "instrument": code}
        for code in ("A", "B")
    ])

    filtered = builder._apply_sample_filters(
        membership,
        date_start="2024-01-04",
        date_end="2024-01-04",
        sample_filters={
            "minimum_listing_trading_days": 0,
            "exclude_st": False,
            "exclude_delisting": False,
            "custom_formulas": [{
                "name": "站上20日均线",
                "expression": "$close > Mean($close, 20) && $amount > 0",
            }],
        },
    )

    assert filtered.to_dict("records") == [{
        "trade_date": pd.Timestamp("2024-01-04"), "instrument": "A",
    }]
    assert builder.client.query_params["source_start"] < "2024-01-04"


def test_sample_filters_read_frozen_fields_from_stock_entity_asset(monkeypatch) -> None:
    captured = {}

    def fake_source(factor, **kwargs):
        captured["factor"] = factor
        captured["source_kwargs"] = kwargs
        return nullcontext(SimpleNamespace())

    def fake_plan(factor, **kwargs):
        captured["plan_factor"] = factor
        captured["plan_kwargs"] = kwargs
        return SimpleNamespace(
            sql="SELECT toDate('2024-01-04') AS trade_date, 'A' AS entity_code, 1 AS score",
            params={"date_start": pd.Timestamp("2024-01-04").date()},
        )

    monkeypatch.setattr(dataset_module, "factor_query_source", fake_source)
    monkeypatch.setattr(dataset_module, "build_factor_query_plan", fake_plan)

    class _Client:
        query_text = ""
        query_params = {}

        def query(self, query, parameters):
            self.query_text = query
            self.query_params = parameters
            return SimpleNamespace(result_rows=[
                (pd.Timestamp("2024-01-04"), "A"),
            ])

    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace(source_database="starlight")
    builder.client = _Client()
    membership = pd.DataFrame([
        {"trade_date": pd.Timestamp("2024-01-04"), "instrument": code}
        for code in ("A", "B")
    ])

    filtered = builder._apply_sample_filters(
        membership,
        date_start="2024-01-04",
        date_end="2024-01-04",
        sample_filters={
            "custom_formulas": [{
                "name": "质量过滤",
                "expression": "$quality_score >= 80",
                "available_fields": [{
                    "field": "quality_score",
                    "label": "质量分",
                    "data_type": "float64",
                    "entity_id": "stock",
                    "asset_id": "stock_quality_daily",
                    "asset_name": "股票质量日频",
                    "asset_updated_at": "2026-08-25T10:00:00Z",
                    "provider_node": "quality-provider",
                }],
            }],
        },
    )

    assert filtered.to_dict("records") == [{
        "trade_date": pd.Timestamp("2024-01-04"), "instrument": "A",
    }]
    assert captured["factor"].required_fields == ["quality_score"]
    assert captured["factor"].params["_force_entity_asset_source"] is True
    assert captured["source_kwargs"]["date_start"].isoformat() == "2024-01-04"
    assert "score != 0" in builder.client.query_text
    assert builder.client.query_params["codes"] == ["A", "B"]


def test_factor_query_calculates_on_demand_without_factor_value_persistence(monkeypatch) -> None:
    class _Client:
        query_text = ""
        query_params = {}

        def query(self, query, parameters):
            self.query_text = query
            self.query_params = parameters
            return SimpleNamespace(result_rows=[
                (pd.Timestamp("2024-01-02"), "000001.SZ", 0.5),
            ])

    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace()
    builder.client = _Client()
    sentinel_cutoff = pd.Timestamp("2024-01-02 15:00:00").to_pydatetime()
    monkeypatch.setattr(
        dataset_module.factor_repository, "get_factor",
        lambda factor_id, version: SimpleNamespace(factor_id=factor_id, version=version),
    )
    monkeypatch.setattr(
        dataset_module, "build_factor_query_plan",
        lambda *args, **kwargs: SimpleNamespace(
            sql="SELECT trade_date, entity_code, score FROM source_daily",
            params={"date_start": kwargs["date_start"], "date_end": kwargs["date_end"]},
            params_hash="a" * 64,
        ),
    )
    monkeypatch.setattr(
        dataset_module, "factor_query_source",
        lambda *args, **kwargs: nullcontext(None),
    )

    frame = builder._factor_values(
        {
            "factor_id": "future_sentinel", "factor_version": 1,
            "params_hash": "a" * 64, "params": {"window": 20},
        },
        sentinel_cutoff, "2024-01-02", "2024-01-02",
    )

    assert len(frame) == 1
    assert "source_daily" in builder.client.query_text
    assert "score AS value" in builder.client.query_text
    assert "factor_values_daily" not in builder.client.query_text
    assert "INSERT" not in builder.client.query_text.upper()


def test_entity_asset_field_query_uses_virtual_definition_without_factor_repository(monkeypatch) -> None:
    class _Client:
        def query(self, query, parameters):
            return SimpleNamespace(result_rows=[
                (pd.Timestamp("2024-01-02"), "000001.SZ", 0.5),
            ])

    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace()
    builder.client = _Client()
    feature = normalize_entity_field_feature({
        "feature_kind": "entity_field",
        "factor_id": "entity_stock_daily_close_1234abcd",
        "entity_id": "stock",
        "asset_id": "asset_stock_daily_stock_daily_real",
        "asset_name": "股票日线数据",
        "asset_updated_at": "2026-08-12T16:14:50+00:00",
        "provider_node": "stock_daily_real",
        "field": "close",
        "label": "收盘价",
        "data_type": "number",
    })
    monkeypatch.setattr(
        dataset_module.factor_repository,
        "get_factor",
        lambda *_args, **_kwargs: pytest.fail("实体字段不应读取因子定义库"),
    )
    captured = {}

    def fake_plan(factor, **kwargs):
        captured["factor"] = factor
        return SimpleNamespace(
            sql="SELECT trade_date, entity_code, score FROM entity_asset_daily",
            params={"date_start": kwargs["date_start"], "date_end": kwargs["date_end"]},
            params_hash="b" * 64,
        )

    monkeypatch.setattr(dataset_module, "build_factor_query_plan", fake_plan)
    monkeypatch.setattr(
        dataset_module, "factor_query_source", lambda *args, **kwargs: nullcontext(None),
    )

    frame = builder._factor_values(
        feature,
        pd.Timestamp("2024-01-02 15:00:00").to_pydatetime(),
        "2024-01-02",
        "2024-01-02",
    )

    assert frame["value"].tolist() == [0.5]
    assert captured["factor"].expression == "$close"
    assert captured["factor"].category == "基础行情"


def test_factor_query_chunks_long_ranges_without_losing_boundaries(monkeypatch) -> None:
    class _Client:
        def __init__(self):
            self.calls = []

        def query(self, query, parameters):
            self.calls.append(dict(parameters))
            return SimpleNamespace(result_rows=[
                (pd.Timestamp(parameters["date_start"]), "000001.SZ", 0.5),
                (pd.Timestamp(parameters["date_end"]), "000002.SZ", -0.5),
            ])

    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace()
    builder.client = _Client()
    monkeypatch.setattr(
        dataset_module.factor_repository, "get_factor",
        lambda factor_id, version: SimpleNamespace(factor_id=factor_id, version=version),
    )
    monkeypatch.setattr(
        dataset_module, "build_factor_query_plan",
        lambda *args, **kwargs: SimpleNamespace(
            sql="SELECT trade_date, entity_code, score FROM source_daily",
            params={"date_start": kwargs["date_start"], "date_end": kwargs["date_end"]},
            params_hash="a" * 64,
        ),
    )
    monkeypatch.setattr(
        dataset_module, "factor_query_source",
        lambda *args, **kwargs: nullcontext(None),
    )

    frame = builder._factor_values(
        {
            "factor_id": "long_window_factor", "factor_version": 1,
            "params_hash": "a" * 64, "params": {},
        },
        pd.Timestamp("2022-12-31 15:00:00").to_pydatetime(),
        "2020-01-01", "2022-12-31",
    )

    assert len(builder.client.calls) == 3
    assert builder.client.calls[0]["date_start"].isoformat() == "2020-01-01"
    assert builder.client.calls[-1]["date_end"].isoformat() == "2022-12-31"
    for left, right in zip(builder.client.calls, builder.client.calls[1:]):
        assert left["date_end"] + pd.Timedelta(days=1) == right["date_start"]
    assert len(frame) == 6


def test_entity_asset_batch_stages_same_source_once_across_output_chunks(
    monkeypatch,
) -> None:
    class _Client:
        def query(self, query, parameters):
            if "SELECT DISTINCT toDate(trade_time)" in query:
                return SimpleNamespace(result_rows=[
                    (pd.Timestamp("2024-01-02"),),
                    (pd.Timestamp("2024-01-03"),),
                ])
            return SimpleNamespace(result_rows=[
                (
                    pd.Timestamp(parameters["output_date"]),
                    "000001.SZ", 1.0, 2.0,
                ),
            ])

    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace(
        source_database="source",
        stock_daily_table="stock_daily",
        factor_database="factor",
        data_sdk_api_base_url="http://data.test",
        data_sdk_query_timeout_seconds=30,
        data_sdk_query_concurrency=2,
    )
    builder.client = _Client()
    builder.factor_query_chunk_days = 90
    staged_calls = []

    def fake_staged_source(**kwargs):
        staged_calls.append(kwargs)
        return nullcontext(SimpleNamespace(
            database="factor",
            table="staged",
            code_column="code",
            date_column="trade_time",
            source_vintage="test",
            date_start=kwargs["date_start"],
            date_end=kwargs["date_end"],
        ))

    monkeypatch.setattr(
        dataset_module, "staged_entity_asset_source", fake_staged_source,
    )
    monkeypatch.setattr(
        dataset_module, "compile_qlib_formula",
        lambda *_args, **_kwargs: SimpleNamespace(max_window=20),
    )
    planned_ranges = []

    def fake_plan(factors, **kwargs):
        planned_ranges.append((
            kwargs["date_start"], kwargs["date_end"],
        ))
        return SimpleNamespace(
            sql="SELECT score",
            params={"output_date": kwargs["date_start"]},
            value_columns=("factor_0", "factor_1"),
            params_hashes=("a" * 64, "b" * 64),
        )

    monkeypatch.setattr(
        dataset_module, "build_factor_score_batch_plan", fake_plan,
    )
    factors = [
        SimpleNamespace(
            factor_id="factor_a", expression="$close",
            entity_type="stock",
            required_fields=["close"],
        ),
        SimpleNamespace(
            factor_id="factor_b", expression="$amount / $volume",
            entity_type="stock",
            required_fields=["amount", "volume"],
        ),
    ]
    items = [
        ({
            "factor_id": "factor_a", "factor_version": 1, "params": {},
            "params_hash": "a" * 64,
        }, factors[0]),
        ({
            "factor_id": "factor_b", "factor_version": 1, "params": {},
            "params_hash": "b" * 64,
        }, factors[1]),
    ]

    frame = builder._factor_values_batch(
        items,
        pd.Timestamp("2025-01-03 15:00:00").to_pydatetime(),
        "2024-01-02",
        "2025-01-03",
        deterministic_wide=True,
    )

    assert len(staged_calls) == 1
    assert staged_calls[0]["fields"] == ["amount", "close", "volume"]
    assert staged_calls[0]["date_end"].isoformat() == "2025-01-03"
    assert planned_ranges == [
        (pd.Timestamp("2024-01-02").date(), pd.Timestamp("2025-01-01").date()),
        (pd.Timestamp("2025-01-02").date(), pd.Timestamp("2025-01-03").date()),
    ]
    assert frame["factor_a__v1__aaaaaaaa"].tolist() == [1.0, 1.0]
    assert frame["factor_b__v1__bbbbbbbb"].tolist() == [2.0, 2.0]
    assert frame["trade_date"].tolist() == [
        pd.Timestamp("2024-01-02"), pd.Timestamp("2025-01-02"),
    ]


def test_entity_asset_batch_groups_stock_composite_assets() -> None:
    daily = SimpleNamespace(
        entity_type="stock",
        params={"_source_asset": "asset_stock_daily"},
    )
    fundamentals = SimpleNamespace(
        entity_type="stock",
        params={"_source_asset": "asset_fundamentals_pit"},
    )

    assert dataset_module._entity_asset_batch_key(daily) == "stock"
    assert dataset_module._entity_asset_batch_key(fundamentals) == "stock"


def test_factor_feature_matrix_joins_all_columns_once_in_expected_order() -> None:
    expected = pd.DataFrame([
        {"trade_date": pd.Timestamp("2024-01-02"), "instrument": "A"},
        {"trade_date": pd.Timestamp("2024-01-02"), "instrument": "B"},
        {"trade_date": pd.Timestamp("2024-01-03"), "instrument": "A"},
    ])
    first = pd.DataFrame([
        {
            "trade_date": pd.Timestamp("2024-01-02"),
            "instrument": "A", "factor_a": 1.0,
        },
        {
            "trade_date": pd.Timestamp("2024-01-03"),
            "instrument": "A", "factor_a": 3.0,
        },
    ])
    second = pd.DataFrame([
        {
            "trade_date": pd.Timestamp("2024-01-02"),
            "instrument": "B", "factor_b": 2.0,
        },
    ])

    result = _assemble_factor_feature_matrix(expected, [first, second])

    assert result[["trade_date", "instrument"]].equals(expected)
    assert result["factor_a"].tolist()[:1] == [1.0]
    assert pd.isna(result.loc[1, "factor_a"])
    assert result.loc[1, "factor_b"] == 2.0
    assert pd.isna(result.loc[2, "factor_b"])


def test_factor_feature_matrix_rejects_duplicate_factor_keys() -> None:
    expected = pd.DataFrame([{
        "trade_date": pd.Timestamp("2024-01-02"), "instrument": "A",
    }])
    duplicated = pd.DataFrame([
        {
            "trade_date": pd.Timestamp("2024-01-02"),
            "instrument": "A", "factor_a": 1.0,
        },
        {
            "trade_date": pd.Timestamp("2024-01-02"),
            "instrument": "A", "factor_a": 2.0,
        },
    ])

    with pytest.raises(ValueError, match="重复日期与股票键"):
        _assemble_factor_feature_matrix(expected, [duplicated])


def test_factor_query_rejects_changed_frozen_params(monkeypatch) -> None:
    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace()
    builder.client = SimpleNamespace(query=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        dataset_module.factor_repository, "get_factor",
        lambda factor_id, version: SimpleNamespace(factor_id=factor_id, version=version),
    )
    monkeypatch.setattr(
        dataset_module, "build_factor_query_plan",
        lambda *args, **kwargs: SimpleNamespace(sql="SELECT 1", params={}, params_hash="b" * 64),
    )
    monkeypatch.setattr(
        dataset_module, "factor_query_source",
        lambda *args, **kwargs: nullcontext(None),
    )

    with pytest.raises(ValueError, match="params_hash"):
        builder._factor_values(
            {"factor_id": "mom", "factor_version": 1, "params_hash": "a" * 64, "params": {}},
            pd.Timestamp("2024-01-02 15:00:00").to_pydatetime(),
            "2024-01-02", "2024-01-02",
        )


def test_future_function_sentinel_allows_only_safe_row() -> None:
    class _Client:
        query_text = ""

        def query(self, query, parameters):
            self.query_text = query
            assert parameters["cutoff"] == pd.Timestamp("2024-01-02 15:00:00").to_pydatetime()
            return SimpleNamespace(result_rows=[(["safe"],)])

    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.client = _Client()

    result = builder.audit_future_function_sentinel()

    assert result["ok"] is True
    assert result["visible_rows"] == ["safe"]
    assert "computed_at <= {cutoff:DateTime}" in builder.client.query_text
    assert "event_available_at <= toDateTime(trade_date, 'Asia/Shanghai')" in builder.client.query_text
