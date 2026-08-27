from __future__ import annotations

import pandas as pd
import pytest

from factor_service.model_backtest import (
    _architecture_walk_forward_backtest,
    _build_top_n_targets,
    _compose_architecture_signals,
    _expand_architecture_prediction_scopes,
)


def _market(calendar: pd.DatetimeIndex, codes: list[str]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "date": trade_date,
            "code": code,
            "forward_return": 0.01,
            "buy_allowed": True,
            "sell_allowed": True,
            "is_st": 0,
            "is_withdrawal": 0,
        }
        for trade_date in calendar
        for code in codes
    ])


def test_top_n_uses_next_session_and_stable_score_order() -> None:
    calendar = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04"])
    codes = ["000001.SZ", "000002.SZ", "000003.SZ"]
    signals = pd.DataFrame({
        "signal_date": [pd.Timestamp("2024-01-02")] * 3,
        "code": codes,
        "score": [0.9, 0.4, 0.9],
    })

    targets, counts = _build_top_n_targets(
        signals, _market(calendar, codes), calendar,
        top_n=2, rebalance_every=1,
        configuration={"exclude_limit_paused": False},
    )

    execution_date = pd.Timestamp("2024-01-03")
    assert targets[execution_date] == {"000001.SZ": 0.5, "000003.SZ": 0.5}
    assert counts[execution_date] == 3


def test_top_n_rebalances_every_five_signal_sessions() -> None:
    calendar = pd.date_range("2024-01-02", periods=8, freq="B")
    codes = ["000001.SZ", "000002.SZ"]
    signals = pd.DataFrame([
        {"signal_date": day, "code": code, "score": float(index)}
        for day in calendar[:6]
        for index, code in enumerate(codes)
    ])

    targets, _ = _build_top_n_targets(
        signals, _market(calendar, codes), calendar,
        top_n=1, rebalance_every=5,
        configuration={"exclude_limit_paused": False},
    )

    assert list(targets) == [calendar[1], calendar[6]]


def _architecture_predictions() -> pd.DataFrame:
    scores = {
        ("model-a", "A"): 0.9,
        ("model-a", "B"): 0.8,
        ("model-a", "C"): 0.1,
        ("model-b", "A"): 0.2,
        ("model-b", "B"): 0.7,
        ("model-b", "C"): 0.95,
    }
    return pd.DataFrame([
        {
            "signal_date": pd.Timestamp("2024-01-02"),
            "code": code,
            "model_id": model_id,
            "model_version": 1,
            "score": score,
        }
        for (model_id, code), score in scores.items()
    ])


def _architecture_engines() -> list[dict]:
    return [
        {
            "engine_key": "first", "model_id": "model-a",
            "model_version": 1, "priority": 1, "enabled": True,
            "weight": 1.0, "score_threshold": 0.0, "top_n": 2,
        },
        {
            "engine_key": "second", "model_id": "model-b",
            "model_version": 1, "priority": 2, "enabled": True,
            "weight": 1.0, "score_threshold": 0.0, "top_n": 2,
        },
    ]


def test_architecture_priority_fills_candidates_in_engine_order() -> None:
    result = _compose_architecture_signals(
        _architecture_predictions(), _architecture_engines(),
        merge_method="priority",
    )

    assert result["code"].tolist() == ["A", "B", "C"]
    assert result["score"].tolist() == [1.0, 0.0, -1.0]


def test_architecture_weighted_score_requires_common_engine_coverage() -> None:
    predictions = _architecture_predictions()
    predictions = predictions[
        ~((predictions["model_id"] == "model-b") & (predictions["code"] == "A"))
    ]
    result = _compose_architecture_signals(
        predictions, _architecture_engines(), merge_method="weighted_score",
    )

    assert result["code"].tolist() == ["B", "C"]
    assert result.set_index("code").loc["B", "score"] == pytest.approx(0.75)


def test_architecture_union_uses_each_engine_top_n_and_deduplicates() -> None:
    result = _compose_architecture_signals(
        _architecture_predictions(), _architecture_engines(),
        merge_method="union",
    )

    assert result["code"].tolist() == ["C", "A", "B"]
    assert result["code"].is_unique
    assert result["score"].between(-1.0, 1.0).all()


def test_hierarchical_architecture_gates_before_stock_ranking() -> None:
    scores = {
        ("industry", "A"): 0.6, ("industry", "B"): 0.7,
        ("industry", "C"): -0.1,
        ("stock", "A"): 0.7, ("stock", "B"): 0.9, ("stock", "C"): 0.8,
    }
    predictions = pd.DataFrame([
        {
            "signal_date": pd.Timestamp("2024-01-02"), "code": code,
            "model_id": model_id, "model_version": 1, "score": score,
        }
        for (model_id, code), score in scores.items()
    ])
    engines = [
        {
            "engine_key": "industry", "role": "industry_rotation",
            "model_id": "industry", "model_version": 1, "priority": 1,
            "enabled": True, "weight": 1.0, "score_threshold": 0.0,
            "top_n": 20,
        },
        {
            "engine_key": "stock", "role": "stock_selection",
            "model_id": "stock", "model_version": 1, "priority": 2,
            "enabled": True, "weight": 1.0, "score_threshold": 0.0,
            "top_n": 1,
        },
    ]

    result = _compose_architecture_signals(
        predictions, engines, merge_method="weighted_score",
        pipeline_mode="hierarchical",
    )

    assert result[["code", "score"]].to_dict("records") == [
        {"code": "B", "score": pytest.approx(0.9)},
        {"code": "A", "score": pytest.approx(0.7)},
    ]
    audit = result.attrs["architecture_gate_audit"]
    assert audit["pipeline_mode"] == "hierarchical"
    assert [item["average_count"] for item in audit["stages"]] == [
        3.0, 2.0, 2.0,
    ]


def test_hierarchical_ablation_allows_industry_gate() -> None:
    gate_scores = {"A": 0.6, "B": 0.7, "C": -0.1}
    predictions = pd.DataFrame([
        {
            "signal_date": pd.Timestamp("2024-01-02"), "code": code,
            "model_id": "gate", "model_version": 1, "score": score,
        }
        for code, score in gate_scores.items()
    ] + [
        {
            "signal_date": pd.Timestamp("2024-01-02"), "code": code,
            "model_id": "stock", "model_version": 1, "score": score,
        }
        for code, score in {"A": 0.7, "B": 0.9, "C": 0.8}.items()
    ])
    engines = [
        {
            "engine_key": "gate", "role": "industry_rotation",
            "model_id": "gate", "model_version": 1, "priority": 1,
            "enabled": True, "weight": 1.0, "score_threshold": 0.0,
            "top_n": 20,
        },
        {
            "engine_key": "stock", "role": "stock_selection",
            "model_id": "stock", "model_version": 1, "priority": 2,
            "enabled": True, "weight": 1.0, "score_threshold": -1.0,
            "top_n": 20,
        },
    ]

    result = _compose_architecture_signals(
        predictions, engines, merge_method="weighted_score",
        pipeline_mode="hierarchical",
    )

    assert result["code"].tolist() == ["B", "A"]
    stages = result.attrs["architecture_gate_audit"]["stages"]
    assert [item["key"] for item in stages] == [
        "input", "industry_gate", "stock_rank",
    ]


def test_industry_prediction_is_broadcast_to_exact_date_members(monkeypatch) -> None:
    predictions = pd.DataFrame([{
        "signal_date": pd.Timestamp("2024-01-02").date(),
        "entity_type": "industry", "code": "801010.SI",
        "model_id": "industry", "model_version": 1, "score": 0.8,
    }])
    engines = [{
        "engine_key": "industry", "role": "industry_rotation",
        "prediction_scope": "industry", "model_id": "industry",
        "model_version": 1, "enabled": True,
    }]

    monkeypatch.setattr(
        "factor_service.model_backtest._industry_membership_for_backtest",
        lambda *_args: pd.DataFrame([
            {
                "signal_date": pd.Timestamp("2024-01-02"),
                "industry_entity": "801010.SI", "code": "A",
            },
            {
                "signal_date": pd.Timestamp("2024-01-02"),
                "industry_entity": "801010.SI", "code": "B",
            },
        ]),
    )

    expanded = _expand_architecture_prediction_scopes(
        predictions, engines,
        date_start=pd.Timestamp("2024-01-02").date(),
        date_end=pd.Timestamp("2024-01-02").date(),
    )

    assert expanded[["code", "score"]].to_dict("records") == [
        {"code": "A", "score": 0.8},
        {"code": "B", "score": 0.8},
    ]


def test_architecture_walk_forward_backtest_reports_complete_oos_windows() -> None:
    windows = [
        ("2024-01-02", "2024-01-08", 0.001),
        ("2024-02-01", "2024-02-07", 0.001),
        ("2024-03-01", "2024-03-07", 0.001),
    ]
    daily = pd.concat([
        pd.DataFrame({
            "trade_date": pd.date_range(start, end, freq="B"),
            "portfolio_return": value,
            "benchmark_return": 0.0,
            "excess_return": value,
            "turnover": 0.1,
        })
        for start, end, value in windows
    ], ignore_index=True)
    contract = {
        "eligible": True,
        "window_count": 3,
        "windows": [
            {"window": index, "test_start": start, "test_end": end}
            for index, (start, end, _value) in enumerate(windows, start=1)
        ],
    }

    report = _architecture_walk_forward_backtest(daily, contract)

    assert report["complete_window_count"] == 3
    assert report["status"] == "stable"
    assert report["aggregate"]["positive_excess_window_ratio"] == pytest.approx(1.0)
    assert report["failed_checks"] == []


def test_architecture_walk_forward_backtest_marks_volatile_windows_mixed() -> None:
    windows = [
        ("2024-01-02", "2024-01-08", -0.001),
        ("2024-02-01", "2024-02-07", 0.001),
        ("2024-03-01", "2024-03-07", 0.001),
    ]
    daily = pd.concat([
        pd.DataFrame({
            "trade_date": pd.date_range(start, end, freq="B"),
            "portfolio_return": value,
            "benchmark_return": 0.0,
            "excess_return": value,
            "turnover": 0.1,
        })
        for start, end, value in windows
    ], ignore_index=True)
    report = _architecture_walk_forward_backtest(daily, {
        "eligible": True, "window_count": 3,
        "windows": [
            {"window": index, "test_start": start, "test_end": end}
            for index, (start, end, _value) in enumerate(windows, start=1)
        ],
    })

    assert report["status"] == "mixed"
    assert "worst_window" in report["failed_checks"]


def test_architecture_walk_forward_backtest_does_not_count_partial_window() -> None:
    daily = pd.DataFrame({
        "trade_date": pd.date_range("2024-01-03", "2024-01-08", freq="B"),
        "portfolio_return": 0.01,
        "benchmark_return": 0.0,
        "excess_return": 0.01,
        "turnover": 0.1,
    })
    report = _architecture_walk_forward_backtest(daily, {
        "eligible": True, "window_count": 1,
        "windows": [{
            "window": 1, "test_start": "2024-01-02", "test_end": "2024-01-08",
        }],
    })

    assert report["evaluated_window_count"] == 1
    assert report["complete_window_count"] == 0
    assert report["status"] == "insufficient"
