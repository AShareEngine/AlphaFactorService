from factor_service.model_validation import assess_model_validation


def _metrics(**changes):
    return {"test_days": 80, "rank_ic": 0.04, "ic_ir": 0.6, **changes}


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
    assert result["failed_checks"] == []


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
