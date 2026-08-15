from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ValidationRule:
    key: str
    label: str
    source: str
    operator: str
    threshold: float
    value_key: str = ""


DEFAULT_VALIDATION_RULES = (
    ValidationRule("validation_days", "验证交易日", "validation", ">=", 40, "days"),
    ValidationRule("validation_rank_ic", "验证RankIC", "validation", ">=", 0.02, "rank_ic"),
    ValidationRule("validation_ic_ir", "验证ICIR", "validation", ">=", 0.30, "ic_ir"),
    ValidationRule("test_days", "独立测试交易日", "metrics", ">=", 40),
    ValidationRule("test_rank_ic", "独立测试RankIC", "metrics", ">=", 0.02, "rank_ic"),
    ValidationRule("test_ic_ir", "独立测试ICIR", "metrics", ">=", 0.30, "ic_ir"),
    ValidationRule("trading_days", "回测交易日", "backtest", ">=", 40),
    ValidationRule("excess_annual_return", "超额年化", "backtest", ">", 0.0),
    ValidationRule("sharpe_ratio", "夏普比率", "backtest", ">", 0.0),
    ValidationRule("max_drawdown", "最大回撤", "backtest", ">=", -0.30),
)

PARAMETER_SELECTION_RULES = (
    ValidationRule("days", "验证交易日", "validation", ">=", 40),
    ValidationRule("rank_ic", "验证RankIC", "validation", ">=", 0.02),
    ValidationRule("ic_ir", "验证ICIR", "validation", ">=", 0.30),
)

WALK_FORWARD_STABILITY_RULES = (
    ValidationRule("window_count", "独立测试窗口", "walk_forward", ">=", 3),
    ValidationRule("window_ic_mean", "窗口平均RankIC", "walk_forward", ">=", 0.02),
    ValidationRule("window_ic_std", "窗口RankIC波动", "walk_forward", "<=", 0.02),
    ValidationRule(
        "positive_ic_window_ratio", "正RankIC窗口占比", "walk_forward", ">=", 0.50,
    ),
    ValidationRule("ic_ir", "拼接样本外ICIR", "walk_forward", ">=", 0.30),
)


def assess_model_validation(
    metrics: Mapping[str, Any] | None,
    backtest: Mapping[str, Any] | Any | None,
) -> dict[str, Any]:
    """Evaluate the default research gate without mutating model state."""
    metric_values = dict(metrics or {})
    if backtest is None:
        backtest_values: dict[str, Any] = {}
        backtest_id = ""
    elif isinstance(backtest, Mapping):
        backtest_values = dict(backtest)
        backtest_id = str(backtest_values.get("backtest_job_id") or "")
    else:
        backtest_values = dict(backtest.model_dump())
        backtest_id = str(getattr(backtest, "backtest_job_id", "") or "")

    checks = []
    for rule in DEFAULT_VALIDATION_RULES:
        if rule.source == "metrics":
            source = metric_values
        elif rule.source == "validation":
            source = dict(metric_values.get("validation") or {})
        else:
            source = backtest_values
        actual = _number(source.get(rule.value_key or rule.key))
        passed = actual is not None and _compare(actual, rule.operator, rule.threshold)
        checks.append({
            "key": rule.key,
            "label": rule.label,
            "source": rule.source,
            "operator": rule.operator,
            "threshold": rule.threshold,
            "actual": actual,
            "passed": passed,
        })
    failed = [item for item in checks if item["passed"] is not True]
    return {
        "policy": "alphablocks.research-gate.v2",
        "passed": not failed,
        "approved": not failed,
        "backtest_job_id": backtest_id,
        "checks": checks,
        "failed_checks": [item["key"] for item in failed],
        "manual_override": False,
        "override_reason": "",
    }


def assess_parameter_trial(
    validation_metrics: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Evaluate one hyper-parameter trial using validation data only."""
    values = dict(validation_metrics or {})
    checks = []
    for rule in PARAMETER_SELECTION_RULES:
        actual = _number(values.get(rule.key))
        passed = actual is not None and _compare(actual, rule.operator, rule.threshold)
        checks.append({
            "key": rule.key,
            "label": rule.label,
            "source": rule.source,
            "operator": rule.operator,
            "threshold": rule.threshold,
            "actual": actual,
            "passed": passed,
        })
    failed = [item for item in checks if item["passed"] is not True]
    return {
        "policy": "alphablocks.parameter-selection.v1",
        "passed": not failed,
        "checks": checks,
        "failed_checks": [item["key"] for item in failed],
    }


def select_parameter_trial(
    jobs: list[Mapping[str, Any]], *, complete: bool,
) -> dict[str, Any]:
    """Select one finalist without consulting test-set or backtest metrics."""
    assessments = []
    observed = []
    qualified = []
    for source in jobs:
        job = dict(source)
        experiment = dict((job.get("config_json") or {}).get("experiment") or {})
        validation_metrics = dict(
            ((job.get("result_json") or {}).get("metrics") or {}).get("validation") or {}
        )
        assessment = assess_parameter_trial(validation_metrics)
        item = {
            "job_id": str(job.get("job_id") or ""),
            "model_id": str(job.get("model_id") or ""),
            "model_version": int(job.get("model_version") or 0),
            "trial_index": int(experiment.get("trial_index") or 0),
            "status": str(job.get("status") or "unknown"),
            "search_params": dict(experiment.get("search_params") or {}),
            "metrics": validation_metrics,
            **assessment,
        }
        assessments.append(item)
        if item["status"] == "succeeded" and _number(validation_metrics.get("rank_ic")) is not None:
            observed.append(item)
            if assessment["passed"]:
                qualified.append(item)

    observed.sort(key=_parameter_trial_sort_key)
    qualified.sort(key=_parameter_trial_sort_key)
    best_observed = observed[0] if observed else None
    selected = qualified[0] if complete and qualified else None
    if not complete:
        status = "evaluating"
    elif selected:
        status = "selected"
    else:
        status = "no_qualified_trials"
    return {
        "policy": "alphablocks.parameter-selection.v1",
        "status": status,
        "complete": bool(complete),
        "ranking_metric": "validation.rank_ic",
        "qualified_count": len(qualified),
        "selected_job_id": selected["job_id"] if selected else "",
        "selected_model_id": selected["model_id"] if selected else "",
        "selected_model_version": selected["model_version"] if selected else 0,
        "selected_trial_index": selected["trial_index"] if selected else 0,
        "best_observed_job_id": best_observed["job_id"] if best_observed else "",
        "best_observed_trial_index": best_observed["trial_index"] if best_observed else 0,
        "best_observed_rank_ic": (
            _number(best_observed["metrics"].get("rank_ic")) if best_observed else None
        ),
        "trial_assessments": assessments,
    }


def assess_walk_forward_stability(
    aggregate: Mapping[str, Any] | None, *, window_count: int,
) -> dict[str, Any]:
    """Interpret stitched OOS windows without changing model approval state."""
    values = {**dict(aggregate or {}), "window_count": int(window_count)}
    checks = []
    for rule in WALK_FORWARD_STABILITY_RULES:
        actual = _number(values.get(rule.key))
        passed = actual is not None and _compare(actual, rule.operator, rule.threshold)
        checks.append({
            "key": rule.key,
            "label": rule.label,
            "source": rule.source,
            "operator": rule.operator,
            "threshold": rule.threshold,
            "actual": actual,
            "passed": passed,
        })
    passed_count = sum(item["passed"] is True for item in checks)
    failed = [item for item in checks if item["passed"] is not True]
    if int(window_count) < 3:
        status = "insufficient_windows"
        conclusion = "独立测试窗口少于3个，暂时不能判断跨期稳定性。"
    elif not failed:
        status = "stable"
        conclusion = "全部稳定性维度达标，样本外信号具备较好的跨期一致性。"
    elif passed_count >= 3:
        status = "mixed"
        conclusion = "多数维度达标，但仍应检查表现最弱的测试窗口。"
    else:
        status = "unstable"
        conclusion = "多个稳定性维度未达标，暂不建议仅凭该模型进入策略验证。"
    return {
        "policy": "alphablocks.walk-forward-stability.v1",
        "status": status,
        "passed": status == "stable",
        "passed_count": passed_count,
        "check_count": len(checks),
        "checks": checks,
        "failed_checks": [item["key"] for item in failed],
        "conclusion": conclusion,
    }


def _parameter_trial_sort_key(item: Mapping[str, Any]) -> tuple[float, float, float, int]:
    metrics = dict(item.get("metrics") or {})
    rank_ic = _number(metrics.get("rank_ic"))
    ic_ir = _number(metrics.get("ic_ir"))
    rmse = _number(metrics.get("rmse"))
    return (
        -(rank_ic if rank_ic is not None else float("-inf")),
        -(ic_ir if ic_ir is not None else float("-inf")),
        rmse if rmse is not None else float("inf"),
        int(item.get("trial_index") or 0),
    )


def _number(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric == numeric and numeric not in {float("inf"), float("-inf")} else None


def _compare(actual: float, operator: str, threshold: float) -> bool:
    if operator == ">":
        return actual > threshold
    if operator == ">=":
        return actual >= threshold
    if operator == "<=":
        return actual <= threshold
    raise ValueError(f"不支持的验证运算符: {operator}")


__all__ = [
    "DEFAULT_VALIDATION_RULES",
    "PARAMETER_SELECTION_RULES",
    "WALK_FORWARD_STABILITY_RULES",
    "assess_model_validation",
    "assess_parameter_trial",
    "assess_walk_forward_stability",
    "select_parameter_trial",
]
