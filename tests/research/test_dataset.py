from __future__ import annotations

import pandas as pd
import pytest
from types import SimpleNamespace

from factor_service.research import dataset as dataset_module
from factor_service.research.dataset import (
    DatasetBuilder,
    _future_rank_label,
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


def test_walk_forward_rolling_windows_use_embargo_and_do_not_overlap_tests() -> None:
    dates = pd.date_range("2018-01-02", periods=1600, freq="B")
    windows = walk_forward_segments(
        pd.Index(dates), train_years=1, valid_months=3,
        test_months=12, step_months=12, max_windows=3, embargo_days=5,
    )

    assert len(windows) == 3
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
    assert "factor_values_daily" not in builder.client.query_text
    assert "INSERT" not in builder.client.query_text.upper()


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
