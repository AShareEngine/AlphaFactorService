from __future__ import annotations

import pytest

from factor_service.research.training_diagnostics import (
    build_training_diagnostics,
    build_training_diagnostics_from_series,
)


def test_tree_training_diagnostics_selects_validation_best_iteration() -> None:
    result = build_training_diagnostics_from_series(
        model_kind="lightgbm",
        metric_name="l2",
        train_values=[0.50, 0.25, 0.10],
        valid_values=[0.52, 0.30, 0.40],
        steps=[1, 2, 3],
        model_params={"num_boost_round": 10, "early_stopping_rounds": 2},
    )

    assert result["available"] is True
    assert result["best_iteration"] == 2
    assert result["best_valid"] == pytest.approx(0.30)
    assert result["validation_rebound_ratio"] == pytest.approx(1 / 3)
    assert result["early_stopped"] is True
    assert result["status"] == "severe"
    assert result["method"]["guard"].endswith("不读取独立测试段或回测收益")


def test_deep_training_diagnostics_preserves_real_evaluation_steps() -> None:
    result = build_training_diagnostics(
        "transformer_lstm",
        {
            "train": [0.30, 0.25, 0.23],
            "valid": [0.31, 0.27, 0.24],
            "steps": [10, 20, 30],
        },
        {"max_steps": 100, "early_stopping_rounds": 5, "eval_steps": 10},
    )

    assert result["metric"] == "loss"
    assert result["trained_iterations"] == 30
    assert result["best_iteration"] == 30
    assert result["early_stopped"] is True
    assert [item["iteration"] for item in result["history"]] == [10, 20, 30]


def test_first_iteration_validation_peak_is_flagged_as_weak_generalization() -> None:
    result = build_training_diagnostics_from_series(
        model_kind="lightgbm",
        metric_name="l2",
        train_values=[0.32, 0.31, 0.30, 0.29],
        valid_values=[0.33, 0.331, 0.332, 0.333],
        steps=[1, 2, 3, 4],
        model_params={"num_boost_round": 1000, "early_stopping_rounds": 3},
    )

    assert result["best_iteration"] == 1
    assert result["validation_improvement_ratio"] == pytest.approx(0.0)
    assert result["status"] == "warning"
    assert result["checks"][2]["passed"] is False
    assert "泛化信号偏弱" in result["conclusion"]
