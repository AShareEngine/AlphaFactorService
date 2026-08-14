from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from factor_service import model_repository


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

    monkeypatch.setattr(model_repository, "client", lambda: _Client())
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


def test_inference_availability_uses_market_database_and_checks_requested_date(monkeypatch) -> None:
    queries = []

    class _Client:
        def query(self, query, parameters):
            queries.append((query, parameters))
            if "factor_values_daily" in query:
                return SimpleNamespace(result_rows=[(date(2026, 8, 12), 1)])
            return SimpleNamespace(result_rows=[(date(2026, 8, 10), 1)])

    monkeypatch.setattr(model_repository, "client", lambda: _Client())
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(clickhouse_database="ab_factor"),
    )

    result = model_repository.model_inference_availability(
        factors=[{
            "factor_id": "period_return",
            "factor_version": 3,
            "params_hash": "frozen-hash",
        }],
        requested_trade_date=date(2026, 8, 10),
        data_cutoff=datetime(2026, 8, 13, 15, 30),
    )

    assert result["trade_date"] == date(2026, 8, 10)
    assert result["requested_trade_date_available"] is True
    assert "starlight.ad_market_kline_daily" in queries[1][0]
    assert "computed_at <= {data_cutoff:DateTime}" in queries[0][0]
    assert "event_available_at <=" in queries[0][0]
    assert queries[1][1]["requested_trade_date"] == date(2026, 8, 10)


def test_inference_dates_requires_every_frozen_factor_and_market_date(monkeypatch) -> None:
    captured = {}

    class _Client:
        def query(self, query, parameters):
            captured["query"] = query
            captured["parameters"] = parameters
            return SimpleNamespace(result_rows=[(date(2026, 8, 10),), (date(2026, 8, 11),)])

    monkeypatch.setattr(model_repository, "client", lambda: _Client())
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(clickhouse_database="ab_factor"),
    )
    rows = model_repository.model_inference_dates(
        factors=[
            {"factor_id": "a", "factor_version": 1, "params_hash": "ha"},
            {"factor_id": "b", "factor_version": 2, "params_hash": "hb"},
        ],
        after_date=date(2026, 8, 9), limit=5,
    )
    assert rows == [date(2026, 8, 10), date(2026, 8, 11)]
    assert "uniqExact(factor_key) = {factor_count:UInt32}" in captured["query"]
    assert "starlight.ad_market_kline_daily" in captured["query"]
    assert captured["parameters"]["factor_count"] == 2
