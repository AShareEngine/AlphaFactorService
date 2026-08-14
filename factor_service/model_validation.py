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


DEFAULT_VALIDATION_RULES = (
    ValidationRule("test_days", "样本外交易日", "metrics", ">=", 40),
    ValidationRule("rank_ic", "RankIC", "metrics", ">=", 0.02),
    ValidationRule("ic_ir", "ICIR", "metrics", ">=", 0.30),
    ValidationRule("trading_days", "回测交易日", "backtest", ">=", 40),
    ValidationRule("excess_annual_return", "超额年化", "backtest", ">", 0.0),
    ValidationRule("sharpe_ratio", "夏普比率", "backtest", ">", 0.0),
    ValidationRule("max_drawdown", "最大回撤", "backtest", ">=", -0.30),
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
        source = metric_values if rule.source == "metrics" else backtest_values
        actual = _number(source.get(rule.key))
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
        "policy": "alphablocks.research-gate.v1",
        "passed": not failed,
        "approved": not failed,
        "backtest_job_id": backtest_id,
        "checks": checks,
        "failed_checks": [item["key"] for item in failed],
        "manual_override": False,
        "override_reason": "",
    }


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
    raise ValueError(f"不支持的验证运算符: {operator}")


__all__ = ["DEFAULT_VALIDATION_RULES", "assess_model_validation"]
