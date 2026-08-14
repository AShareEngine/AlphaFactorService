from __future__ import annotations

import pandas as pd
import pytest
from types import SimpleNamespace

from factor_service.research.dataset import DatasetBuilder, _future_rank_label, split_trading_dates
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


def test_dataset_build_checks_cancellation_before_clickhouse_query() -> None:
    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace()
    builder._membership = lambda *_args: (_ for _ in ()).throw(AssertionError("query must not run"))
    cancellation = CancellationToken()
    cancellation.cancel("stop")

    with pytest.raises(JobCanceled, match="stop"):
        builder.build(valid_job(), cancellation=cancellation)


def test_factor_query_enforces_event_and_computation_cutoffs() -> None:
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
    builder.settings = SimpleNamespace(factor_database="ab_factor")
    builder.client = _Client()
    sentinel_cutoff = pd.Timestamp("2024-01-02 15:00:00").to_pydatetime()

    frame = builder._factor_values(
        {"factor_id": "future_sentinel", "factor_version": 1, "params_hash": "a" * 64},
        sentinel_cutoff, "2024-01-02", "2024-01-02",
    )

    assert len(frame) == 1
    assert "computed_at <= {cutoff:DateTime}" in builder.client.query_text
    assert "event_available_at <= toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR" in builder.client.query_text
    assert builder.client.query_params["cutoff"] == sentinel_cutoff


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
