from __future__ import annotations

import pandas as pd
import pytest
from types import SimpleNamespace

from factor_service.research import dataset as dataset_module
from factor_service.research.dataset import (
    DatasetBuilder,
    _future_rank_label,
    _industry_features,
    _industry_rank_label,
    _market_style_features,
    _market_style_rank_label,
    split_trading_dates,
    walk_forward_segments,
)
from factor_service.research.errors import JobCanceled
from factor_service.research.job import CancellationToken
from tests.research.utils import valid_job


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


def test_market_style_features_use_daily_pit_market_cap_halves() -> None:
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    features = pd.DataFrame([
        {"trade_date": day, "instrument": code, "factor": value}
        for day, rows in (
            (dates[0], (("A", 1.0), ("B", 3.0), ("C", 5.0), ("D", 7.0))),
            (dates[1], (("A", 2.0), ("B", 4.0), ("C", 6.0), ("D", 8.0))),
        )
        for code, value in rows
    ])
    market_caps = pd.DataFrame([
        {"trade_date": day, "instrument": code, "market_cap": cap}
        for day, rows in (
            (dates[0], (("A", 10), ("B", 20), ("C", 30), ("D", 40))),
            # B和C在第二天交换大小盘归属，证明分组不是静态标签。
            (dates[1], (("A", 10), ("B", 40), ("C", 20), ("D", 50))),
        )
        for code, cap in rows
    ])

    aggregated, membership = _market_style_features(
        features, market_caps, ["factor"],
    )

    first = aggregated[aggregated["trade_date"] == dates[0]].set_index("instrument")
    second = aggregated[aggregated["trade_date"] == dates[1]].set_index("instrument")
    assert first.loc["STYLE_SMALL", "factor"] == pytest.approx(2.0)
    assert first.loc["STYLE_LARGE", "factor"] == pytest.approx(6.0)
    assert second.loc["STYLE_SMALL", "factor"] == pytest.approx(4.0)
    assert second.loc["STYLE_LARGE", "factor"] == pytest.approx(6.0)
    second_membership = membership[
        membership["trade_date"] == dates[1]
    ].set_index("instrument")["style_entity"]
    assert second_membership.to_dict() == {
        "A": "STYLE_SMALL", "C": "STYLE_SMALL",
        "B": "STYLE_LARGE", "D": "STYLE_LARGE",
    }


def test_market_style_label_is_centered_between_small_and_large_groups() -> None:
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    prices = pd.DataFrame([
        {"trade_date": day, "instrument": code, "adjusted_close": value}
        for day, rows in (
            (dates[0], (("A", 10.0), ("B", 10.0), ("C", 10.0), ("D", 10.0))),
            (dates[1], (("A", 12.0), ("B", 11.0), ("C", 9.0), ("D", 8.0))),
            (dates[2], (("A", 12.0), ("B", 11.0), ("C", 9.0), ("D", 8.0))),
        )
        for code, value in rows
    ])
    membership = pd.DataFrame([
        {"trade_date": dates[0], "instrument": code, "style_entity": style}
        for code, style in (
            ("A", "STYLE_SMALL"), ("B", "STYLE_SMALL"),
            ("C", "STYLE_LARGE"), ("D", "STYLE_LARGE"),
        )
    ])

    labels = _market_style_rank_label(prices, membership, horizon=1)
    first = labels.set_index("instrument")["LABEL0"]

    assert first["STYLE_SMALL"] == pytest.approx(1.0)
    assert first["STYLE_LARGE"] == pytest.approx(-1.0)


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


def test_split_has_five_session_embargo_between_segments() -> None:
    dates = pd.date_range("2024-01-02", periods=100, freq="B")
    segments = split_trading_dates(pd.Index(dates), embargo_days=5)

    train_end = dates.get_loc(pd.Timestamp(segments["train"][1]))
    valid_start = dates.get_loc(pd.Timestamp(segments["valid"][0]))
    valid_end = dates.get_loc(pd.Timestamp(segments["valid"][1]))
    test_start = dates.get_loc(pd.Timestamp(segments["test"][0]))
    assert valid_start - train_end == 6
    assert test_start - valid_end == 6


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


def test_walk_forward_rolling_windows_use_embargo_and_do_not_overlap_tests() -> None:
    dates = pd.date_range("2018-01-02", periods=1600, freq="B")
    windows = walk_forward_segments(
        pd.Index(dates), train_years=1, valid_months=3,
        test_months=12, step_months=12, max_windows=3, embargo_days=5,
    )

    assert len(windows) == 3
    assert windows[-1]["test"][1] == dates[-1].date().isoformat()
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


def test_walk_forward_expanding_windows_keep_original_train_start() -> None:
    dates = pd.date_range("2018-01-02", periods=1600, freq="B")
    windows = walk_forward_segments(
        pd.Index(dates), strategy="expanding", train_years=1,
        valid_months=3, test_months=6, step_months=6, max_windows=3,
    )

    assert len(windows) == 3
    assert {window["train"][0] for window in windows} == {dates[0].date().isoformat()}
    assert windows[0]["train"][1] < windows[-1]["train"][1]
    assert windows[-1]["test"][1] == dates[-1].date().isoformat()


def test_walk_forward_rejects_overlapping_test_windows() -> None:
    dates = pd.date_range("2018-01-02", periods=1000, freq="B")
    with pytest.raises(ValueError, match="步长不得小于测试窗口"):
        walk_forward_segments(
            pd.Index(dates), train_years=1, valid_months=3,
            test_months=12, step_months=6,
        )


def test_dataset_build_checks_cancellation_before_clickhouse_query() -> None:
    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace()
    builder._membership = lambda *_args: (_ for _ in ()).throw(AssertionError("query must not run"))
    cancellation = CancellationToken()
    cancellation.cancel("stop")

    with pytest.raises(JobCanceled, match="stop"):
        builder.build(valid_job(), cancellation=cancellation)


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
