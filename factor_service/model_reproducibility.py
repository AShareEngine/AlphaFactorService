from __future__ import annotations

import math
from typing import Any, Mapping


def build_model_reproducibility_audit(
    source_model: Mapping[str, Any],
    replay_model: Mapping[str, Any],
    prediction_audit: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an immutable exact-replay audit from control and prediction data."""
    origin = dict((replay_model.get("job_config_json") or {}).get("research_origin") or {})
    source_job_id = str(source_model.get("job_id") or "")
    checks = [
        _check(
            "origin_mode", "服务端复用类型",
            origin.get("mode") == "exact_replay",
            str(origin.get("mode") or "未记录"),
        ),
        _check(
            "source_job", "来源训练任务",
            str(origin.get("source_job_id") or "") == source_job_id,
            str(origin.get("source_job_id") or "未记录"),
        ),
        _check(
            "source_model", "来源模型版本",
            (
                str(origin.get("source_model_id") or "")
                == str(source_model.get("model_id") or "")
                and int(origin.get("source_model_version") or 0)
                == int(source_model.get("version") or 0)
            ),
            (
                f"{origin.get('source_model_id') or '--'}"
                f" · v{int(origin.get('source_model_version') or 0)}"
            ),
        ),
        _check(
            "dataset_hash", "Dataset Hash",
            bool(source_model.get("dataset_hash"))
            and str(source_model.get("dataset_hash")) == str(replay_model.get("dataset_hash")),
            str(replay_model.get("dataset_hash") or "未记录"),
        ),
        _check(
            "source_dataset_hash", "来源 Dataset 声明",
            bool(origin.get("source_dataset_hash"))
            and str(origin.get("source_dataset_hash")) == str(source_model.get("dataset_hash")),
            str(origin.get("source_dataset_hash") or "未记录"),
        ),
        _check(
            "source_config_hash", "来源配置 Hash",
            bool(origin.get("source_config_hash")),
            str(origin.get("source_config_hash") or "未记录"),
        ),
    ]
    metric_audit = compare_reproducibility_metrics(
        source_model.get("metrics_json") or {}, replay_model.get("metrics_json") or {},
    )
    control_passed = all(item["passed"] for item in checks)
    prediction_status = str(prediction_audit.get("status") or "unavailable")
    if control_passed and metric_audit["status"] == "exact" and prediction_status == "exact":
        status = "exact"
    elif (
        control_passed
        and metric_audit["passed"] is True
        and prediction_audit.get("passed") is True
    ):
        status = "equivalent"
    elif not control_passed or metric_audit["status"] == "drifted":
        status = "drifted"
    elif prediction_status == "unavailable":
        status = "unavailable"
    else:
        status = "drifted"
    conclusions = {
        "exact": "冻结配置、研究指标和逐行预测完全一致，本次训练已复现来源模型。",
        "equivalent": "冻结配置一致，结果仅存在浮点容差内差异，可视为数值等价复现。",
        "drifted": "冻结配置虽然声明一致，但指标或预测已经发生实质偏移，请检查运行环境、随机性和依赖版本。",
        "unavailable": "冻结配置已核对，但预测结果尚不可用于逐行复现审计。",
    }
    return {
        "schema_version": "alphablocks.model-reproducibility-audit.v1",
        "status": status,
        "passed": status in {"exact", "equivalent"},
        "conclusion": conclusions[status],
        "source": {
            "model_id": str(source_model.get("model_id") or ""),
            "model_version": int(source_model.get("version") or 0),
            "job_id": source_job_id,
            "dataset_hash": str(source_model.get("dataset_hash") or ""),
        },
        "replay": {
            "model_id": str(replay_model.get("model_id") or ""),
            "model_version": int(replay_model.get("version") or 0),
            "job_id": str(replay_model.get("job_id") or ""),
            "dataset_hash": str(replay_model.get("dataset_hash") or ""),
        },
        "configuration_checks": checks,
        "metrics": metric_audit,
        "predictions": dict(prediction_audit),
    }


def compare_reproducibility_metrics(
    source: Mapping[str, Any], replay: Mapping[str, Any],
    *, absolute_tolerance: float = 1e-10, relative_tolerance: float = 1e-8,
) -> dict[str, Any]:
    left = _numeric_metric_paths(source)
    right = _numeric_metric_paths(replay)
    rows = []
    for path in sorted(set(left) | set(right)):
        source_value = left.get(path)
        replay_value = right.get(path)
        available = source_value is not None and replay_value is not None
        absolute_delta = abs(source_value - replay_value) if available else None
        scale = max(abs(source_value), abs(replay_value), 1.0) if available else None
        relative_delta = absolute_delta / scale if available and scale else None
        exact = available and absolute_delta <= 1e-12
        passed = available and (
            absolute_delta <= absolute_tolerance
            or relative_delta <= relative_tolerance
        )
        rows.append({
            "path": path,
            "source": source_value,
            "replay": replay_value,
            "absolute_delta": absolute_delta,
            "relative_delta": relative_delta,
            "exact": exact,
            "passed": passed,
        })
    if not rows:
        status = "unavailable"
    elif all(item["exact"] for item in rows):
        status = "exact"
    elif all(item["passed"] for item in rows):
        status = "equivalent"
    else:
        status = "drifted"
    failed = [item for item in rows if not item["passed"]]
    return {
        "status": status,
        "passed": status in {"exact", "equivalent"},
        "compared_count": len(rows),
        "failed_count": len(failed),
        "max_absolute_delta": max(
            (item["absolute_delta"] for item in rows if item["absolute_delta"] is not None),
            default=None,
        ),
        "absolute_tolerance": absolute_tolerance,
        "relative_tolerance": relative_tolerance,
        "differences": failed[:50],
        "values": rows,
    }


def _numeric_metric_paths(source: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}

    def visit(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                next_path = f"{path}.{key}" if path else str(key)
                visit(child, next_path)
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        numeric = float(value)
        if math.isfinite(numeric):
            result[path] = numeric

    visit(source, "")
    return result


def _check(key: str, label: str, passed: bool, value: str) -> dict[str, Any]:
    return {"key": key, "label": label, "passed": bool(passed), "value": value}
