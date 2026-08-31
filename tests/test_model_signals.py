from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from factor_service import model_repository


def test_model_score_snapshot_requires_exact_date_and_stable_order(monkeypatch) -> None:
    target_date = date(2026, 8, 28)
    cutoff = datetime(2026, 8, 28, 15, 0)
    responses = [
        [(4000,)],
        [
            ("000002.SZ", 0.9, 0.8, 1, 1.0, cutoff, cutoff, "v1", "hash", "run"),
            ("000001.SZ", 0.7, 0.6, 2, 0.5, cutoff, cutoff, "v1", "hash", "run"),
        ],
    ]

    class _Client:
        def query(self, _query, parameters):
            assert parameters["trade_date"] == target_date
            return SimpleNamespace(result_rows=responses.pop(0))

    monkeypatch.setattr(model_repository, "client", lambda: _Client())
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="ab_model"),
    )

    result = model_repository.model_score_snapshot(
        model_id="model-a", model_version=4, trade_date=target_date, topk=1,
    )

    assert result["row_count"] == 2
    assert result["returned_count"] == 1
    assert result["rows"][0]["entity_code"] == "000002.SZ"


def test_stock_market_history_uses_real_ohlc_and_latest_display_name(monkeypatch) -> None:
    calls = []

    class _Client:
        def query(self, query, parameters):
            calls.append((query, parameters))
            if "ad_market_kline_daily" in query:
                return SimpleNamespace(result_rows=[
                    (date(2025, 12, 30), 9.79, 9.80, 9.68, 9.74, 100.0, 974.0),
                    (date(2025, 12, 31), 9.74, 9.81, 9.70, 9.70, 120.0, 1164.0),
                ])
            return SimpleNamespace(result_rows=[("长沙银行", date(2026, 7, 30))])

    monkeypatch.setattr(model_repository, "client", lambda: _Client())

    result = model_repository.stock_market_history(
        "601577.SH", through_date="2025-12-31", limit=60,
    )

    assert result["entity_name"] == "长沙银行"
    assert result["name_snapshot_date"] == "2026-07-30"
    assert result["rows"][-1]["close"] == 9.70
    assert calls[0][1]["through_date"] == date(2025, 12, 31)


def test_formal_strategy_signal_query_is_pit_safe_and_top_n(monkeypatch) -> None:
    captured = {}

    class _Client:
        def query(self, query, parameters):
            captured["query"] = query
            captured["parameters"] = parameters
            return SimpleNamespace(result_rows=[
                (
                    date(2026, 8, 13), "000001.SZ", 0.99, 1,
                    __import__("datetime").datetime(2026, 8, 13, 15, 0),
                    "a" * 64, "model_job_daily",
                ),
            ])

    fake = _Client()
    monkeypatch.setattr(model_repository, "client", lambda: fake)
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="ab_model"),
    )

    rows = model_repository.list_model_signals(
        model_id="model-a", model_version=1,
        trade_date=date(2026, 8, 13), top_n=20,
    )

    assert rows[0].entity_code == "000001.SZ"
    assert "feature_cutoff_at <=" in captured["query"]
    assert captured["parameters"]["top_n"] == 20


def test_prediction_query_without_date_returns_only_latest_cross_section(monkeypatch) -> None:
    captured = {}

    class _Client:
        def query(self, query, parameters):
            captured["query"] = query
            captured["parameters"] = parameters
            return SimpleNamespace(result_rows=[])

    monkeypatch.setattr(model_repository, "client", lambda: _Client())
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="ab_model"),
    )

    rows = model_repository.list_model_predictions(
        model_id="model-a", model_version=1, limit=1000,
    )

    assert rows == []
    assert "trade_date = (" in captured["query"]
    assert "SELECT max(trade_date)" in captured["query"]
    assert captured["parameters"]["limit"] == 1000


def test_prediction_query_for_entity_returns_cross_date_history(monkeypatch) -> None:
    captured = {}

    class _Client:
        def query(self, query, parameters):
            captured["query"] = query
            captured["parameters"] = parameters
            return SimpleNamespace(result_rows=[])

    monkeypatch.setattr(model_repository, "client", lambda: _Client())
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="ab_model"),
    )

    rows = model_repository.list_model_predictions(
        model_id="model-a", model_version=1,
        entity_code="000001.SZ", limit=120,
    )

    assert rows == []
    assert "entity_code = {entity_code:String}" in captured["query"]
    assert "SELECT max(trade_date)" not in captured["query"]
    assert captured["parameters"]["entity_code"] == "000001.SZ"
    assert captured["parameters"]["limit"] == 120


def test_prediction_overview_reports_distribution_and_cross_day_stability(
    monkeypatch,
) -> None:
    selected_date = date(2024, 1, 3)
    previous_date = date(2024, 1, 2)
    cutoff = datetime(2024, 1, 3, 15, 0)
    previous_cutoff = datetime(2024, 1, 2, 15, 0)
    date_rows = [
        (selected_date, 5, 4, 1, 1),
        (previous_date, 5, 5, 1, 1),
    ]
    prediction_rows = [
        (selected_date, "A", 0.8, 1, 1.0, 1.0, cutoff, cutoff, "hash", "run-2", "v2"),
        (selected_date, "B", 0.5, 2, 0.8, 0.5, cutoff, cutoff, "hash", "run-2", "v2"),
        (selected_date, "C", 0.2, 3, 0.6, 0.0, cutoff, cutoff, "hash", "run-2", "v2"),
        (selected_date, "D", -0.2, 4, 0.4, -0.5, cutoff, cutoff, "hash", "run-2", "v2"),
        (selected_date, "E", -0.6, 5, 0.1, -1.0, cutoff, cutoff, "hash", "run-2", "v2"),
        (previous_date, "B", 0.7, 1, 1.0, 1.0, previous_cutoff, previous_cutoff, "hash", "run-1", "v1"),
        (previous_date, "A", 0.6, 2, 0.8, 0.5, previous_cutoff, previous_cutoff, "hash", "run-1", "v1"),
        (previous_date, "C", 0.1, 3, 0.6, 0.0, previous_cutoff, previous_cutoff, "hash", "run-1", "v1"),
        (previous_date, "E", -0.2, 4, 0.4, -0.5, previous_cutoff, previous_cutoff, "hash", "run-1", "v1"),
        (previous_date, "F", -0.7, 5, 0.1, -1.0, previous_cutoff, previous_cutoff, "hash", "run-1", "v1"),
    ]
    responses = [date_rows, prediction_rows]

    class _Client:
        def query(self, _query, _parameters=None, **_kwargs):
            return SimpleNamespace(result_rows=responses.pop(0))

    monkeypatch.setattr(model_repository, "client", lambda: _Client())
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="ab_model"),
    )

    result = model_repository.model_prediction_overview(
        model_id="model-a", model_version=1, top_n=2,
    )

    assert result["selected_date"] == selected_date
    assert result["cross_section"]["row_count"] == 5
    assert result["cross_section"]["pit_violation_count"] == 1
    assert result["score_stats"]["p50"] == 0.0
    assert sum(item["count"] for item in result["score_histogram"]) == 5
    assert result["top_candidates"][0]["entity_code"] == "A"
    assert result["stability"]["previous_date"] == previous_date
    assert result["stability"]["common_entities"] == 4
    assert result["stability"]["top_n_overlap_count"] == 2
    assert result["stability"]["top_n_overlap_ratio"] == 1.0
    assert result["stability"]["new_entrants"] == []


def test_prediction_stability_caps_top_n_to_available_cross_section() -> None:
    selected_date = date(2024, 1, 3)
    previous_date = date(2024, 1, 2)
    selected = pd.DataFrame({
        "entity_code": ["A", "B", "C"],
        "rank_value": [1, 2, 3],
        "score": [1.0, 0.0, -1.0],
    })
    previous = pd.DataFrame({
        "entity_code": ["A", "B", "D"],
        "rank_value": [1, 2, 3],
        "score": [1.0, 0.0, -1.0],
    })

    result = model_repository._prediction_rank_stability(
        selected,
        previous,
        selected_date=selected_date,
        previous_date=previous_date,
        top_n=20,
    )

    assert result["top_n"] == 20
    assert result["comparison_top_n"] == 3
    assert result["top_n_overlap_count"] == 2
    assert result["top_n_overlap_ratio"] == pytest.approx(2 / 3)


def test_stock_return_calibration_uses_same_percentile_bucket_and_realized_paths() -> None:
    rows = []
    for day_index in range(12):
        signal_date = pd.Timestamp("2025-01-02") + pd.Timedelta(day_index, unit="D")
        for stock_index in range(10):
            rows.append({
                "trade_date": signal_date,
                "instrument": f"S{stock_index:02d}",
                "percentile": 0.91 + stock_index * 0.008,
                "score": 0.8 + stock_index * 0.01,
                "rank_value": stock_index + 1,
                "return_t_plus_1": 0.01 + stock_index * 0.001,
                "return_t_plus_2": 0.02 + stock_index * 0.001,
                "return_t_plus_3": 0.03 + stock_index * 0.001,
            })
    aligned = pd.DataFrame(rows)

    result = model_repository._stock_return_calibration_from_frames(
        aligned=aligned,
        model_id="model-a",
        model_version=1,
        entity_code="S00",
        as_of_date=date(2025, 1, 20),
        horizon=3,
        buckets=10,
        target_percentile=0.95,
        target_bucket=10,
        current_score=0.9,
        current_rank=1,
        minimum_samples=80,
        minimum_signal_days=10,
    )

    assert result["status"] == "ready"
    assert result["available"] is True
    assert result["calibration"]["sample_count"] == 120
    assert result["calibration"]["signal_days"] == 12
    assert result["returns"]["p50"] == pytest.approx(0.0345)
    assert result["curve"][-1]["p50_return"] == pytest.approx(0.0345)
    assert result["method"]["native_quantile_model"] is False


def test_stock_return_calibration_never_emits_numbers_for_insufficient_history() -> None:
    aligned = pd.DataFrame([
        {
            "trade_date": pd.Timestamp("2025-01-02"),
            "instrument": "S00",
            "percentile": 0.95,
            "score": 0.9,
            "rank_value": 1,
            "return_t_plus_5": 0.03,
        },
    ])

    result = model_repository._stock_return_calibration_from_frames(
        aligned=aligned,
        model_id="model-a",
        model_version=1,
        entity_code="S00",
        as_of_date=date(2025, 1, 20),
        horizon=5,
        buckets=10,
        target_percentile=0.95,
        target_bucket=10,
        current_score=0.9,
        current_rank=1,
        minimum_samples=80,
        minimum_signal_days=10,
    )

    assert result["status"] == "insufficient_history"
    assert result["available"] is False
    assert result["returns"]["p50"] is None
    assert result["curve"] == []


def test_inference_availability_uses_market_database_and_checks_requested_date(monkeypatch) -> None:
    queries = []

    class _Client:
        def query(self, query, parameters):
            queries.append((query, parameters))
            return SimpleNamespace(result_rows=[(date(2026, 8, 10), 1)])

    monkeypatch.setattr(model_repository, "client", lambda: _Client())
    monkeypatch.setattr(model_repository, "_validate_frozen_factors", lambda factors: None)
    monkeypatch.setattr(
        model_repository, "_factor_source_ready_dates",
        lambda **kwargs: [kwargs.get("before_date")],
    )

    result = model_repository.model_inference_availability(
        factors=[{
            "factor_id": "period_return",
            "factor_version": 3,
            "params_hash": "frozen-hash",
            "params": {"window": 20},
        }],
        requested_trade_date=date(2026, 8, 10),
        data_cutoff=datetime(2026, 8, 13, 15, 30),
    )

    assert result["trade_date"] == date(2026, 8, 10)
    assert result["requested_trade_date_available"] is True
    assert "starlight.ad_market_kline_daily" in queries[0][0]
    assert "factor_values_daily" not in queries[0][0]
    assert queries[0][1]["requested_trade_date"] == date(2026, 8, 10)


def test_inference_availability_falls_back_from_partial_latest_source_day(monkeypatch) -> None:
    class _Client:
        def query(self, query, parameters):
            return SimpleNamespace(result_rows=[(date(2026, 8, 13), 1)])

    monkeypatch.setattr(model_repository, "client", lambda: _Client())
    monkeypatch.setattr(model_repository, "_validate_frozen_factors", lambda factors: None)
    monkeypatch.setattr(
        model_repository,
        "_factor_source_ready_dates",
        lambda **kwargs: (
            [date(2026, 8, 12)] if kwargs.get("after_date") is None else []
        ),
    )

    result = model_repository.model_inference_availability(
        factors=[{
            "factor_id": "btop", "factor_version": 1,
            "params_hash": "frozen-hash", "params": {},
        }],
        requested_trade_date=date(2026, 8, 13),
        data_cutoff=datetime(2026, 8, 15, 12, 0),
    )

    assert result["market_latest_date"] == date(2026, 8, 13)
    assert result["factor_latest_date"] == date(2026, 8, 12)
    assert result["trade_date"] == date(2026, 8, 12)
    assert result["requested_trade_date_available"] is False


def test_inference_dates_requires_every_frozen_factor_and_market_date(monkeypatch) -> None:
    captured = {}

    class _Client:
        def query(self, query, parameters):
            captured["query"] = query
            captured["parameters"] = parameters
            return SimpleNamespace(result_rows=[(date(2026, 8, 10),), (date(2026, 8, 11),)])

    monkeypatch.setattr(model_repository, "client", lambda: _Client())
    monkeypatch.setattr(model_repository, "_validate_frozen_factors", lambda factors: None)
    monkeypatch.setattr(
        model_repository, "_factor_source_ready_dates",
        lambda **_kwargs: [date(2026, 8, 10), date(2026, 8, 11)],
    )
    rows = model_repository.model_inference_dates(
        factors=[
            {"factor_id": "a", "factor_version": 1, "params_hash": "ha", "params": {}},
            {"factor_id": "b", "factor_version": 2, "params_hash": "hb", "params": {}},
        ],
        after_date=date(2026, 8, 9), limit=5,
    )
    assert rows == [date(2026, 8, 10), date(2026, 8, 11)]


def test_inference_ready_dates_filter_partial_source_vintage(monkeypatch) -> None:
    captured = {}

    class _Client:
        def query(self, query, parameters):
            captured["query"] = query
            captured["parameters"] = parameters
            return SimpleNamespace(result_rows=[(date(2026, 8, 12),)])

    factors = {
        "btop": SimpleNamespace(factor_id="btop", required_fields=["pb"]),
        "earnings_to_price_ratio": SimpleNamespace(
            factor_id="earnings_to_price_ratio", required_fields=["pe"],
        ),
    }
    monkeypatch.setattr(model_repository, "client", lambda: _Client())
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(
            source_database="ab_factor",
            stock_daily_table="stock_daily_factor_source",
            stock_code_column="code",
            stock_date_column="trade_time",
        ),
    )
    monkeypatch.setattr(
        model_repository.factor_repository, "get_factor",
        lambda factor_id, version: factors[factor_id],
    )

    rows = model_repository._factor_source_ready_dates(
        factors=[
            {"factor_id": "btop", "factor_version": 1},
            {"factor_id": "earnings_to_price_ratio", "factor_version": 1},
        ],
        after_date=None,
        before_date=date(2026, 8, 13),
        limit=1,
        descending=True,
    )

    assert rows == [date(2026, 8, 12)]
    assert "source.pb" in captured["query"]
    assert "source.pe" in captured["query"]
    assert "starlight.ad_index_constituent" in captured["query"]
    assert captured["parameters"]["minimum_coverage_0"] == pytest.approx(0.8)


def test_ensemble_availability_requires_every_source_model(monkeypatch) -> None:
    captured = {}

    class _Client:
        def query(self, query, parameters):
            captured["query"] = query
            captured["parameters"] = parameters
            return SimpleNamespace(result_rows=[(
                date(2026, 8, 13), date(2026, 8, 1), 9, 4200, 480,
            )])

    monkeypatch.setattr(model_repository, "client", lambda: _Client())
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="ab_model"),
    )
    result = model_repository.ensemble_prediction_availability(
        sources=[
            {"model_id": "lgbm", "model_version": 1, "weight": 0.4},
            {"model_id": "xgb", "model_version": 2, "weight": 0.6},
        ],
        requested_trade_date=date(2026, 8, 13),
    )

    assert result["requested_trade_date_available"] is True
    assert result["requested_row_count"] == 480
    assert "uniqExact(tuple(model_id, model_version))" in captured["query"]
    assert captured["parameters"]["source_count"] == 2


def test_ensemble_prediction_dates_are_common_and_ordered(monkeypatch) -> None:
    captured = {}

    class _Client:
        def query(self, query, parameters):
            captured["query"] = query
            captured["parameters"] = parameters
            return SimpleNamespace(result_rows=[
                (date(2026, 8, 12),), (date(2026, 8, 13),),
            ])

    monkeypatch.setattr(model_repository, "client", lambda: _Client())
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="ab_model"),
    )
    rows = model_repository.ensemble_prediction_dates(
        sources=[
            {"model_id": "lgbm", "model_version": 1, "weight": 0.5},
            {"model_id": "xgb", "model_version": 1, "weight": 0.5},
        ],
        after_date=date(2026, 8, 11), before_date=date(2026, 8, 14), limit=5,
    )

    assert rows == [date(2026, 8, 12), date(2026, 8, 13)]
    assert "GROUP BY trade_date, entity_code" in captured["query"]
    assert captured["parameters"]["limit"] == 5


def test_model_prediction_comparison_recomputes_metrics_on_common_rows(monkeypatch) -> None:
    source_rows = [
        ("lgbm", 1, date(2026, 1, 2), "A", 1.0),
        ("lgbm", 1, date(2026, 1, 2), "B", 0.0),
        ("lgbm", 1, date(2026, 1, 2), "C", -1.0),
        ("xgb", 1, date(2026, 1, 2), "A", 1.0),
        ("xgb", 1, date(2026, 1, 2), "B", -1.0),
        ("xgb", 1, date(2026, 1, 2), "C", 0.0),
        ("lgbm", 1, date(2026, 1, 3), "A", 1.0),
        ("lgbm", 1, date(2026, 1, 3), "B", 0.0),
        ("lgbm", 1, date(2026, 1, 3), "C", -1.0),
        ("xgb", 1, date(2026, 1, 3), "A", 1.0),
        ("xgb", 1, date(2026, 1, 3), "B", -1.0),
        ("xgb", 1, date(2026, 1, 3), "C", 0.0),
    ]
    price_rows = [
        (date(2026, 1, 2), "A", 1.0), (date(2026, 1, 2), "B", 1.0),
        (date(2026, 1, 2), "C", 1.0), (date(2026, 1, 3), "A", 1.3),
        (date(2026, 1, 3), "B", 1.1), (date(2026, 1, 3), "C", 0.9),
        (date(2026, 1, 4), "A", 1.69), (date(2026, 1, 4), "B", 1.21),
        (date(2026, 1, 4), "C", 0.81),
    ]

    class _Client:
        def __init__(self):
            self.calls = 0

        def query(self, query, parameters):
            self.calls += 1
            return SimpleNamespace(
                result_rows=source_rows if self.calls == 1 else price_rows,
            )

    fake = _Client()
    monkeypatch.setattr(model_repository, "client", lambda: fake)
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="ab_model"),
    )
    result = model_repository.model_prediction_comparison(
        sources=[
            {"model_id": "lgbm", "model_version": 1, "weight": 1},
            {"model_id": "xgb", "model_version": 1, "weight": 1},
        ],
        horizon=1,
    )

    assert result["common_rows"] == 6
    assert result["common_days"] == 2
    assert result["evaluation_rows"] == 6
    assert result["evaluation_days"] == 2
    assert result["correlation_matrix"][0][1] == pytest.approx(0.5)
    assert result["metrics"][0]["rank_ic"] == pytest.approx(1.0)


def test_ensemble_materialization_uses_frozen_weights_and_reranks(monkeypatch) -> None:
    captured = {}

    class _Client:
        def command(self, query, parameters):
            captured["command"] = query
            captured["parameters"] = parameters

        def query(self, query, parameters):
            captured["summary"] = query
            return SimpleNamespace(result_rows=[(
                960, date(2026, 8, 12), date(2026, 8, 13), 2,
            )])

    monkeypatch.setattr(model_repository, "client", lambda: _Client())
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="ab_model"),
    )
    result = model_repository.materialize_ensemble_predictions(
        model_id="ensemble-a", model_version=1,
        sources=[
            {"model_id": "lgbm", "model_version": 1, "weight": 0.25},
            {"model_id": "xgb", "model_version": 2, "weight": 0.75},
        ],
        dataset_hash="a" * 64,
        inference_run_prefix="ensemble_a_",
    )

    assert result["row_count"] == 960
    assert result["date_count"] == 2
    assert "row_number() OVER" in captured["command"]
    assert "2.0 * percentile - 1.0" in captured["command"]
    assert captured["parameters"]["source_weight_0"] == 0.25
    assert captured["parameters"]["source_weight_1"] == 0.75


def test_ensemble_evaluation_uses_realized_future_cross_sectional_labels(monkeypatch) -> None:
    prediction_rows = [
        (date(2026, 1, 2), "A", 1.0), (date(2026, 1, 2), "B", -1.0),
        (date(2026, 1, 3), "A", 1.0), (date(2026, 1, 3), "B", -1.0),
    ]
    price_rows = [
        (date(2026, 1, 2), "A", 1.0), (date(2026, 1, 2), "B", 1.0),
        (date(2026, 1, 3), "A", 2.0), (date(2026, 1, 3), "B", 1.0),
        (date(2026, 1, 4), "A", 4.0), (date(2026, 1, 4), "B", 1.0),
    ]

    class _Client:
        def __init__(self):
            self.calls = 0

        def query(self, query, parameters):
            self.calls += 1
            return SimpleNamespace(
                result_rows=prediction_rows if self.calls == 1 else price_rows,
            )

    fake = _Client()
    monkeypatch.setattr(model_repository, "client", lambda: fake)
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="ab_model"),
    )
    result = model_repository.evaluate_model_predictions(
        model_id="ensemble-a", model_version=1, horizon=1,
    )

    assert result["test_rows"] == 4
    assert result["test_days"] == 2
    assert result["rank_ic"] == pytest.approx(1.0)
    assert result["evaluation_source"] == "realized_future_cross_sectional_rank"


def test_prediction_quantiles_use_realized_returns_and_are_monotonic(monkeypatch) -> None:
    dates = list(pd.date_range("2026-01-02", periods=8, freq="B"))
    instruments = [f"S{index:02d}" for index in range(50)]
    prediction_rows = [
        (trade_date.date(), instrument, float(index))
        for trade_date in dates[:-1]
        for index, instrument in enumerate(instruments)
    ]
    daily_returns = np.linspace(-0.01, 0.02, len(instruments))
    price_rows = [
        (
            trade_date.date(), instrument,
            float(100.0 * (1.0 + daily_returns[index]) ** day),
        )
        for day, trade_date in enumerate(dates)
        for index, instrument in enumerate(instruments)
    ]
    captured = {}

    class _Client:
        def __init__(self):
            self.calls = 0

        def query(self, query, parameters):
            self.calls += 1
            captured.setdefault("queries", []).append(query)
            return SimpleNamespace(
                result_rows=prediction_rows if self.calls == 1 else price_rows,
            )

    fake = _Client()
    monkeypatch.setattr(model_repository, "client", lambda: fake)
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="ab_model"),
    )

    result = model_repository.model_prediction_quantile_diagnostics(
        model_id="model-a", model_version=2, horizon=1, quantiles=5,
    )

    assert result["test_days"] == 7
    assert result["status"] == "strong"
    assert result["aggregate_monotonicity"] == pytest.approx(1.0)
    assert result["adjacent_consistency"] == pytest.approx(1.0)
    assert result["top_bottom_spread_mean"] > 0
    assert result["top_bottom_positive_ratio"] == pytest.approx(1.0)
    assert len(result["groups"]) == 5
    assert "feature_cutoff_at" in captured["queries"][0]
    assert "不等同于可成交策略回测" in result["method"]["disclosure"]


def test_prediction_quantiles_support_non_overlapping_trading_day_sampling(
    monkeypatch,
) -> None:
    dates = list(pd.date_range("2026-01-02", periods=15, freq="B"))
    instruments = [f"S{index:02d}" for index in range(50)]
    predictions = pd.DataFrame([
        {
            "trade_date": trade_date,
            "instrument": instrument,
            "prediction": float(index),
        }
        for trade_date in dates
        for index, instrument in enumerate(instruments)
    ])
    labels = pd.DataFrame([
        {
            "trade_date": trade_date,
            "instrument": instrument,
            "label": (2.0 * index / 49.0) - 1.0,
            "forward_return": (-0.01 + 0.02 * index / 49.0) * (1.0 + day * 0.03),
        }
        for day, trade_date in enumerate(dates)
        for index, instrument in enumerate(instruments)
    ])
    monkeypatch.setattr(
        model_repository, "_pit_safe_model_prediction_frame", lambda **_: predictions,
    )
    monkeypatch.setattr(
        model_repository, "_realized_label_frame", lambda **_: labels,
    )

    result = model_repository.model_prediction_quantile_diagnostics(
        model_id="model-a", model_version=2, horizon=5,
        quantiles=5, sample_interval=5,
    )

    assert result["test_days"] == 3
    assert result["sample_interval_trading_days"] == 5
    assert result["overlap_factor"] == pytest.approx(1.0)
    assert result["effective_test_days"] == pytest.approx(3.0)
    assert result["newey_west_lag"] == 0
    assert result["top_bottom_spread_t_stat"] is not None
    assert result["top_bottom_spread_significance"] == "small_sample"
    assert "相邻样本窗口不重叠" in result["method"]["disclosure"]


def test_prediction_stability_diagnostics_detect_recent_rank_ic_reversal(
    monkeypatch,
) -> None:
    dates = list(pd.date_range("2026-01-02", periods=30, freq="B"))
    instruments = [f"S{index:02d}" for index in range(50)]
    predictions = pd.DataFrame([
        {
            "trade_date": trade_date,
            "instrument": instrument,
            "prediction": float(index),
        }
        for trade_date in dates
        for index, instrument in enumerate(instruments)
    ])
    labels = pd.DataFrame([
        {
            "trade_date": trade_date,
            "instrument": instrument,
            "label": (
                (2.0 * index / 49.0) - 1.0
                if day < 20 else 1.0 - (2.0 * index / 49.0)
            ),
            "forward_return": (
                -0.02 + 0.04 * index / 49.0
                if day < 20 else 0.02 - 0.04 * index / 49.0
            ),
        }
        for day, trade_date in enumerate(dates)
        for index, instrument in enumerate(instruments)
    ])
    monkeypatch.setattr(
        model_repository, "_pit_safe_model_prediction_frame", lambda **_: predictions,
    )
    monkeypatch.setattr(
        model_repository, "_realized_label_frame", lambda **_: labels,
    )

    result = model_repository.model_prediction_stability_diagnostics(
        model_id="model-a", model_version=2, horizon=5,
        rolling_window=10, quantiles=5,
    )

    assert result["status"] == "unstable"
    assert result["test_days"] == 30
    assert result["newey_west_lag"] == 4
    assert result["effective_test_days"] == pytest.approx(6.0)
    assert result["rank_ic_significance"] == "small_sample"
    assert result["early_rank_ic_mean"] == pytest.approx(1.0)
    assert result["recent_rank_ic_mean"] == pytest.approx(-1.0)
    assert result["recent_minus_early_rank_ic"] == pytest.approx(-2.0)
    assert len(result["windows"]) == 3
    assert len(result["daily"]) == 30
    assert result["daily"][-1]["rolling_rank_ic"] == pytest.approx(-1.0)
    assert "不重新训练" in result["method"]["scope"]
    assert "Newey-West lag=4" in result["method"]["statistical_guard"]


def test_prediction_exposure_diagnostics_separate_size_and_industry_pit(
    monkeypatch,
) -> None:
    dates = list(pd.date_range("2026-01-02", periods=5, freq="B"))
    instruments = [f"S{index:02d}" for index in range(50)]
    predictions = pd.DataFrame([
        {
            "trade_date": trade_date,
            "instrument": instrument,
            "prediction": float(index),
        }
        for trade_date in dates
        for index, instrument in enumerate(instruments)
    ])
    labels = pd.DataFrame([
        {
            "trade_date": trade_date,
            "instrument": instrument,
            "label": (2.0 * index / 49.0) - 1.0,
            "forward_return": -0.02 + 0.04 * index / 49.0,
        }
        for trade_date in dates
        for index, instrument in enumerate(instruments)
    ])
    market_caps = pd.DataFrame([
        {
            "trade_date": trade_date,
            "instrument": instrument,
            "market_cap": float((index + 1) * 1_000_000_000),
            "log_market_cap": float(np.log((index + 1) * 1_000_000_000)),
            "equity_available_date": trade_date - timedelta(days=30),
        }
        for trade_date in dates
        for index, instrument in enumerate(instruments)
    ])
    industries = pd.DataFrame([
        {
            "trade_date": trade_date,
            "instrument": instrument,
            "industry": "银行" if index >= 40 else "制造",
        }
        for trade_date in dates
        for index, instrument in enumerate(instruments)
    ])
    monkeypatch.setattr(
        model_repository, "_pit_safe_model_prediction_frame", lambda **_: predictions,
    )
    monkeypatch.setattr(
        model_repository, "_realized_label_frame", lambda **_: labels,
    )
    monkeypatch.setattr(
        model_repository, "_historical_market_cap_frame", lambda **_: market_caps,
    )
    monkeypatch.setattr(
        model_repository, "_historical_industry_mapping", lambda **_: industries,
    )

    result = model_repository.model_prediction_exposure_diagnostics(
        model_id="model-a", model_version=2, horizon=5, score_quantiles=5,
    )

    assert result["status"] == "warning"
    assert result["market_cap"]["coverage_ratio"] == pytest.approx(1.0)
    assert result["market_cap"]["mean_daily_score_log_cap_spearman"] == pytest.approx(1.0)
    assert len(result["market_cap"]["matrix"]) == 25
    largest_cap = result["market_cap"]["exposure"][-1]
    assert largest_cap["top_quantile_weight"] == pytest.approx(1.0)
    assert result["industry"]["coverage_ratio"] == pytest.approx(1.0)
    bank = next(row for row in result["industry"]["exposure"] if row["industry"] == "银行")
    assert bank["universe_weight"] == pytest.approx(0.2)
    assert bank["top_quantile_weight"] == pytest.approx(1.0)
    assert result["industry"]["max_absolute_active_weight"] == pytest.approx(0.8)
    assert "公告日和变更日" in result["method"]["cap_pit_guard"]
    assert "不满足完整PIT可用性" in result["method"]["industry_disclosure"]


def test_raw_prediction_distribution_diagnostics_detect_output_collapse(
    monkeypatch,
) -> None:
    dates = list(pd.date_range("2026-01-02", periods=30, freq="B"))
    instruments = [f"S{index:02d}" for index in range(50)]
    rows = []
    base = np.linspace(-1.0, 1.0, len(instruments))
    for day, trade_date in enumerate(dates):
        values = base if day < 10 else base + 0.05 if day < 20 else base * 0.2 + 1.0
        rows.extend({
            "trade_date": trade_date,
            "instrument": instrument,
            "raw_prediction": float(values[index]),
        } for index, instrument in enumerate(instruments))
    predictions = pd.DataFrame(rows)
    monkeypatch.setattr(
        model_repository, "_pit_safe_raw_prediction_frame", lambda **_: predictions,
    )

    result = model_repository.model_prediction_distribution_diagnostics(
        model_id="model-a", model_version=2, bins=10,
    )

    assert result["status"] == "severe"
    assert result["days"] == 30
    assert len(result["windows"]) == 3
    assert result["recent_to_early_std_ratio"] == pytest.approx(0.2)
    assert result["latest_psi_vs_early"] >= 0.25
    assert len(result["histogram"]) == 20
    assert "raw_prediction" in result["method"]["source"]
    assert "天然接近均匀分布" in result["method"]["why_raw_prediction"]


def test_ensemble_diagnostics_use_common_pit_safe_oos_rows(monkeypatch) -> None:
    source_rows = [
        ("lgbm", 1, date(2026, 1, 2), "A", 1.0),
        ("lgbm", 1, date(2026, 1, 2), "B", 0.0),
        ("lgbm", 1, date(2026, 1, 2), "C", -1.0),
        ("xgb", 2, date(2026, 1, 2), "A", 1.0),
        ("xgb", 2, date(2026, 1, 2), "B", -1.0),
        ("xgb", 2, date(2026, 1, 2), "C", 0.0),
        ("lgbm", 1, date(2026, 1, 3), "A", 1.0),
        ("lgbm", 1, date(2026, 1, 3), "B", 0.0),
        ("lgbm", 1, date(2026, 1, 3), "C", -1.0),
        ("xgb", 2, date(2026, 1, 3), "A", 1.0),
        ("xgb", 2, date(2026, 1, 3), "B", -1.0),
        ("xgb", 2, date(2026, 1, 3), "C", 0.0),
    ]
    price_rows = [
        (date(2026, 1, 2), "A", 1.0),
        (date(2026, 1, 2), "B", 1.0),
        (date(2026, 1, 2), "C", 1.0),
        (date(2026, 1, 3), "A", 1.3),
        (date(2026, 1, 3), "B", 1.1),
        (date(2026, 1, 3), "C", 0.9),
        (date(2026, 1, 4), "A", 1.69),
        (date(2026, 1, 4), "B", 1.21),
        (date(2026, 1, 4), "C", 0.81),
    ]
    queries = []

    class _Client:
        def query(self, query, parameters):
            queries.append(query)
            return SimpleNamespace(
                result_rows=source_rows if len(queries) == 1 else price_rows,
            )

    fake_diagnostics_client = _Client()
    monkeypatch.setattr(
        model_repository, "client", lambda: fake_diagnostics_client,
    )
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="ab_model"),
    )
    result = model_repository.ensemble_model_diagnostics(
        sources=[
            {
                "model_id": "lgbm", "model_version": 1,
                "name": "LightGBM", "model_kind": "lightgbm", "weight": 0.5,
            },
            {
                "model_id": "xgb", "model_version": 2,
                "name": "XGBoost", "model_kind": "xgboost", "weight": 0.5,
            },
        ],
        horizon=1,
    )

    assert result["evaluation_scope"] == "common_pit_safe_oos"
    assert result["usage"] == "diagnostic_only_not_for_weight_selection"
    assert result["test_rows"] == 6
    assert result["test_days"] == 2
    assert result["correlation_matrix"][0][0] == 1.0
    assert result["correlation_matrix"][0][1] == pytest.approx(0.5)
    assert len(result["marginal_contributions"]) == 2
    assert len(result["weight_sensitivity"]) == 5
    assert sum(item["is_baseline"] for item in result["weight_sensitivity"]) == 1
    assert result["time_stability"]["status"] == "insufficient_windows"
    assert "feature_cutoff_at <=" in queries[0]


def test_ensemble_time_stability_uses_chronological_oos_windows() -> None:
    source_keys = ["lgbm::v1", "xgb::v1"]
    rows = []
    for trade_date in pd.bdate_range("2026-01-02", periods=60):
        for instrument, value in (("A", 1.0), ("B", 0.0), ("C", -1.0)):
            rows.append({
                "trade_date": trade_date,
                "instrument": instrument,
                "label": value,
                source_keys[0]: value,
                source_keys[1]: value,
            })
    result = model_repository._ensemble_time_stability(
        aligned=pd.DataFrame(rows),
        sources=[
            {
                "source_key": source_keys[0], "model_id": "lgbm",
                "model_version": 1, "name": "LightGBM", "weight": 0.5,
            },
            {
                "source_key": source_keys[1], "model_id": "xgb",
                "model_version": 1, "name": "XGBoost", "weight": 0.5,
            },
        ],
        source_keys=source_keys,
        source_weights=np.asarray([0.5, 0.5]),
    )

    assert result["window_count"] == 3
    assert result["status"] == "stable"
    assert result["positive_rank_ic_window_ratio"] == 1.0
    assert [window["days"] for window in result["windows"]] == [20, 20, 20]
    assert result["windows"][0]["date_end"] < result["windows"][1]["date_start"]
