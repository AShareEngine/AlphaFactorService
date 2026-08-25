from factor_service.model_validation import (
    assess_model_validation,
    assess_parameter_trial,
    assess_walk_forward_stability,
    select_model_trial,
    select_parameter_trial,
)


def _metrics(**changes):
    return {
        "test_days": 80,
        "rank_ic": 0.04,
        "ic_ir": 0.6,
        "validation": {"days": 60, "rank_ic": 0.03, "ic_ir": 0.5},
        **changes,
    }


def _backtest(**changes):
    return {
        "backtest_job_id": "model_backtest_test",
        "trading_days": 80,
        "excess_annual_return": 0.08,
        "sharpe_ratio": 0.7,
        "max_drawdown": -0.12,
        **changes,
    }


def test_positive_model_passes_research_gate() -> None:
    result = assess_model_validation(_metrics(), _backtest())

    assert result["passed"] is True
    assert result["policy"] == "alphablocks.research-gate.v2"
    assert result["failed_checks"] == []


def test_strong_test_cannot_hide_failed_validation_split() -> None:
    result = assess_model_validation(
        _metrics(validation={"days": 60, "rank_ic": -0.01, "ic_ir": -0.1}),
        _backtest(),
    )

    assert result["passed"] is False
    assert result["failed_checks"] == ["validation_rank_ic", "validation_ic_ir"]


def test_successful_backtest_with_negative_excess_stays_candidate() -> None:
    result = assess_model_validation(
        _metrics(), _backtest(excess_annual_return=-0.01),
    )

    assert result["passed"] is False
    assert result["failed_checks"] == ["excess_annual_return"]


def test_missing_backtest_metrics_cannot_pass_gate() -> None:
    result = assess_model_validation(_metrics(), None)

    assert result["passed"] is False
    assert set(result["failed_checks"]) == {
        "trading_days", "excess_annual_return", "sharpe_ratio", "max_drawdown",
    }


def _trial(
    index: int, *, rank_ic: float, ic_ir: float = 0.5, days: int = 60,
    rmse: float = 0.5, status: str = "succeeded",
) -> dict:
    return {
        "job_id": f"job-{index}",
        "model_id": "demo-grid",
        "model_version": index,
        "status": status,
        "config_json": {"experiment": {
            "trial_index": index,
            "search_params": {"num_leaves": 15 * index},
        }},
        "result_json": {"metrics": {"validation": {
            "days": days, "rank_ic": rank_ic, "ic_ir": ic_ir, "rmse": rmse,
        }}},
    }


def test_parameter_trial_uses_validation_thresholds() -> None:
    result = assess_parameter_trial({"days": 60, "rank_ic": 0.03, "ic_ir": 0.2})

    assert result["passed"] is False
    assert result["failed_checks"] == ["ic_ir"]


def test_parameter_selection_waits_for_every_trial_to_finish() -> None:
    result = select_parameter_trial(
        [_trial(1, rank_ic=0.04), _trial(2, rank_ic=0.05, status="running")],
        complete=False,
    )

    assert result["status"] == "evaluating"
    assert result["selected_job_id"] == ""
    assert result["best_observed_job_id"] == "job-1"


def test_parameter_selection_chooses_best_qualified_validation_rank_ic() -> None:
    result = select_parameter_trial([
        _trial(1, rank_ic=0.04, ic_ir=0.4),
        _trial(2, rank_ic=0.06, ic_ir=0.2),
        _trial(3, rank_ic=0.05, ic_ir=0.6),
    ], complete=True)

    assert result["status"] == "selected"
    assert result["qualified_count"] == 2
    assert result["selected_job_id"] == "job-3"
    assert result["selected_model_version"] == 3
    assert result["best_observed_job_id"] == "job-2"


def test_parameter_selection_returns_no_finalist_when_gate_fails() -> None:
    result = select_parameter_trial([
        _trial(1, rank_ic=-0.01), _trial(2, rank_ic=0.01),
    ], complete=True)

    assert result["status"] == "no_qualified_trials"
    assert result["selected_job_id"] == ""


def test_model_selection_matches_quantmind_absolute_validation_icir() -> None:
    result = select_model_trial([
        _trial(1, rank_ic=0.07, ic_ir=0.25),
        _trial(2, rank_ic=0.03, ic_ir=-0.72),
        _trial(3, rank_ic=0.05, ic_ir=0.61),
    ], complete=True)

    assert result["status"] == "selected"
    assert result["ranking_metric"] == "abs(validation.ic_ir)"
    assert result["selected_job_id"] == "job-2"
    assert result["selected_model_version"] == 2
    assert result["best_observed_ic_ir"] == -0.72


def test_model_selection_waits_for_every_model_to_finish() -> None:
    result = select_model_trial([
        _trial(1, rank_ic=0.04, ic_ir=0.40),
        _trial(2, rank_ic=0.05, ic_ir=0.55, status="running"),
    ], complete=False)

    assert result["status"] == "evaluating"
    assert result["selected_job_id"] == ""
    assert result["best_observed_job_id"] == "job-1"


def test_walk_forward_stability_requires_consistent_oos_windows() -> None:
    result = assess_walk_forward_stability({
        "window_ic_mean": 0.04,
        "window_ic_std": 0.015,
        "positive_ic_window_ratio": 0.75,
        "ic_ir": 0.5,
    }, window_count=4)

    assert result["status"] == "stable"
    assert result["passed"] is True
    assert result["failed_checks"] == []


def test_walk_forward_stability_does_not_treat_one_window_as_evidence() -> None:
    result = assess_walk_forward_stability({
        "window_ic_mean": 0.08,
        "window_ic_std": 0.0,
        "positive_ic_window_ratio": 1.0,
        "ic_ir": 0.8,
    }, window_count=1)

    assert result["status"] == "insufficient_windows"
    assert result["passed"] is False
    assert result["failed_checks"] == ["window_count"]


def test_walk_forward_stability_rejects_negative_or_volatile_signal() -> None:
    result = assess_walk_forward_stability({
        "window_ic_mean": -0.01,
        "window_ic_std": 0.05,
        "positive_ic_window_ratio": 0.25,
        "ic_ir": -0.2,
    }, window_count=4)

    assert result["status"] == "unstable"
    assert set(result["failed_checks"]) == {
        "window_ic_mean", "window_ic_std", "positive_ic_window_ratio", "ic_ir",
    }
