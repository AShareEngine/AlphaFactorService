from __future__ import annotations

from typing import Any

import numpy as np


def build_training_diagnostics(
    model_kind: str,
    evals_result: dict[str, Any],
    model_params: dict[str, Any],
) -> dict[str, Any]:
    """Normalize framework-specific evaluation history into one research report."""
    metric_name, train_values, valid_values = _evaluation_series(evals_result)
    if not train_values or not valid_values:
        return _unavailable(model_kind, model_params)
    raw_steps = evals_result.get("steps")
    if isinstance(raw_steps, list) and len(raw_steps) >= min(len(train_values), len(valid_values)):
        steps = [int(value) for value in raw_steps]
    else:
        interval = int(model_params.get("eval_steps") or 1) if model_kind in {
            "mlp", "lstm", "transformer_lstm",
        } else 1
        steps = [(index + 1) * interval for index in range(min(len(train_values), len(valid_values)))]
    return build_training_diagnostics_from_series(
        model_kind=model_kind,
        metric_name=metric_name,
        train_values=train_values,
        valid_values=valid_values,
        steps=steps,
        model_params=model_params,
    )


def build_training_diagnostics_from_series(
    *,
    model_kind: str,
    metric_name: str,
    train_values: list[Any],
    valid_values: list[Any],
    steps: list[Any],
    model_params: dict[str, Any],
) -> dict[str, Any]:
    count = min(len(train_values), len(valid_values), len(steps))
    points = []
    for index in range(count):
        train = _finite(train_values[index])
        valid = _finite(valid_values[index])
        step = int(steps[index])
        if train is None or valid is None or step < 0:
            continue
        points.append({
            "iteration": step,
            "train": train,
            "valid": valid,
            "gap": valid - train,
        })
    if not points:
        return _unavailable(model_kind, model_params)
    best_index = min(range(len(points)), key=lambda index: points[index]["valid"])
    best = points[best_index]
    final = points[-1]
    denominator = max(abs(best["valid"]), 1e-12)
    gap_ratio = max(0.0, (best["valid"] - best["train"]) / denominator)
    rebound_ratio = max(0.0, (final["valid"] - best["valid"]) / denominator)
    first_valid = float(points[0]["valid"])
    validation_improvement_ratio = max(
        0.0, (first_valid - best["valid"]) / max(abs(first_valid), 1e-12),
    )
    weak_validation_improvement = bool(
        len(points) >= 3 and validation_improvement_ratio < 0.001
    )
    configured_iterations = _configured_iterations(model_kind, model_params)
    trained_iterations = int(final["iteration"])
    early_stopped = bool(
        configured_iterations and trained_iterations < configured_iterations
    )
    if gap_ratio >= 0.50 or rebound_ratio >= 0.20:
        status = "severe"
        conclusion = "训练与验证损失分化明显，存在较高过拟合风险，建议降低复杂度或加强正则。"
    elif weak_validation_improvement:
        status = "warning"
        conclusion = "验证损失从首个评估点起没有实质改善，模型可能过早达到峰值或特征泛化信号偏弱。"
    elif gap_ratio >= 0.20 or rebound_ratio >= 0.05:
        status = "warning"
        conclusion = "训练过程出现一定泛化差距，建议结合Walk-Forward和样本外RankIC继续观察。"
    else:
        status = "stable"
        conclusion = "训练与验证曲线差距处于可接受范围，未观察到明显过拟合信号。"
    sampled = _downsample(points, best_index, maximum=300)
    return {
        "available": True,
        "model_kind": model_kind,
        "metric": str(metric_name or "loss"),
        "direction": "minimize",
        "configured_iterations": configured_iterations,
        "trained_iterations": trained_iterations,
        "best_iteration": int(best["iteration"]),
        "early_stopping_rounds": int(model_params.get("early_stopping_rounds") or 0),
        "early_stopped": early_stopped,
        "best_train": float(best["train"]),
        "best_valid": float(best["valid"]),
        "final_train": float(final["train"]),
        "final_valid": float(final["valid"]),
        "generalization_gap": float(best["valid"] - best["train"]),
        "generalization_gap_ratio": float(gap_ratio),
        "validation_rebound_ratio": float(rebound_ratio),
        "validation_improvement_ratio": float(validation_improvement_ratio),
        "evaluations_after_best": int(len(points) - best_index - 1),
        "status": status,
        "conclusion": conclusion,
        "history": sampled,
        "history_point_count": len(points),
        "history_downsampled": len(sampled) < len(points),
        "checks": [
            {
                "key": "generalization_gap_ratio",
                "label": "最佳轮次泛化差距",
                "actual": float(gap_ratio),
                "threshold": 0.35,
                "operator": "<=",
                "passed": gap_ratio <= 0.35,
            },
            {
                "key": "validation_rebound_ratio",
                "label": "最佳轮次后验证反弹",
                "actual": float(rebound_ratio),
                "threshold": 0.10,
                "operator": "<=",
                "passed": rebound_ratio <= 0.10,
            },
            {
                "key": "validation_improvement_ratio",
                "label": "验证损失有效改善",
                "actual": float(validation_improvement_ratio),
                "threshold": 0.001,
                "operator": ">=",
                "passed": not weak_validation_improvement,
            },
        ],
        "method": {
            "scope": "final_model_train_and_validation",
            "selection": "仅由验证损失选择最佳轮次",
            "guard": "训练曲线用于风险诊断，不读取独立测试段或回测收益",
        },
    }


def _evaluation_series(
    evals_result: dict[str, Any],
) -> tuple[str, list[Any], list[Any]]:
    train_payload = evals_result.get("train")
    valid_payload = evals_result.get("valid")
    if isinstance(train_payload, dict) and isinstance(valid_payload, dict):
        shared = [name for name in train_payload if name in valid_payload]
        if not shared:
            return "loss", [], []
        metric = next(
            (name for name in ("l2", "rmse", "loss") if name in shared), shared[0],
        )
        return metric, list(train_payload.get(metric) or []), list(valid_payload.get(metric) or [])
    if isinstance(train_payload, list) and isinstance(valid_payload, list):
        metric = "loss" if "steps" in evals_result else "rmse"
        return metric, list(train_payload), list(valid_payload)
    return "loss", [], []


def _configured_iterations(model_kind: str, params: dict[str, Any]) -> int:
    if model_kind in {"mlp", "lstm", "transformer_lstm"}:
        return int(params.get("max_steps") or 0)
    return int(params.get("num_boost_round") or params.get("n_estimators") or 0)


def _downsample(
    points: list[dict[str, float | int]], best_index: int, *, maximum: int,
) -> list[dict[str, float | int]]:
    if len(points) <= maximum:
        return points
    selected = set(np.linspace(0, len(points) - 1, maximum, dtype=int).tolist())
    selected.update({0, best_index, len(points) - 1})
    return [points[index] for index in sorted(selected)]


def _finite(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if np.isfinite(numeric) else None


def _unavailable(model_kind: str, model_params: dict[str, Any]) -> dict[str, Any]:
    return {
        "available": False,
        "model_kind": model_kind,
        "configured_iterations": _configured_iterations(model_kind, model_params),
        "early_stopping_rounds": int(model_params.get("early_stopping_rounds") or 0),
        "history": [],
        "status": "unavailable",
        "conclusion": "该历史模型未保存逐轮训练与验证指标；重新训练后会自动生成训练过程诊断。",
        "method": {
            "scope": "final_model_train_and_validation",
            "guard": "训练曲线用于风险诊断，不读取独立测试段或回测收益",
        },
    }


__all__ = [
    "build_training_diagnostics",
    "build_training_diagnostics_from_series",
]
