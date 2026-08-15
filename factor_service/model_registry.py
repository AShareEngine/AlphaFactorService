from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence


def build_model_leaderboard(models: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    normalized = [dict(item) for item in models]
    active = [item for item in normalized if _registry_stage(item) != "archived"]
    cohorts: dict[str, dict[str, Any]] = {}
    for model in active:
        spec = dict(model.get("dataset_spec") or {})
        label = dict(spec.get("label") or {})
        definition = {
            "research_target": str(spec.get("research_target") or "stock_selection"),
            "prediction_scope": str(spec.get("prediction_scope") or "stock"),
            "universe_id": str(spec.get("universe_id") or "csi500"),
            "label_kind": str(label.get("kind") or ""),
            "label_horizon_trading_days": int(label.get("horizon_trading_days") or 5),
            "date_start": str(spec.get("date_start") or "")[:10],
            "date_end": str(spec.get("date_end") or "")[:10],
        }
        encoded = json.dumps(
            definition, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        cohort_id = f"cohort_{sha256(encoded.encode('utf-8')).hexdigest()[:16]}"
        cohort = cohorts.setdefault(cohort_id, {
            "cohort_id": cohort_id,
            **definition,
            "models": [],
        })
        cohort["models"].append(_leaderboard_row(model))

    rows: list[dict[str, Any]] = []
    cohort_rows: list[dict[str, Any]] = []
    for cohort in cohorts.values():
        ranked = sorted(
            cohort.pop("models"),
            key=lambda item: (
                -_number(item.get("validation_rank_ic"), float("-inf")),
                -_number(item.get("validation_ic_ir"), float("-inf")),
                str(item.get("model_id") or ""),
                -int(item.get("model_version") or 0),
            ),
        )
        evaluated_count = sum(
            item.get("validation_rank_ic") is not None for item in ranked
        )
        evaluated_rank = 0
        for item in ranked:
            if item.get("validation_rank_ic") is not None:
                evaluated_rank += 1
                cohort_rank: int | None = evaluated_rank
            else:
                cohort_rank = None
            item.update({
                "cohort_id": cohort["cohort_id"],
                "cohort_rank": cohort_rank,
                "cohort_size": evaluated_count,
            })
            rows.append(item)
        cohort_rows.append({
            **cohort,
            "model_count": len(ranked),
            "evaluated_count": evaluated_count,
            "eligible_count": sum(bool(item["validation_gate_passed"]) for item in ranked),
            "leader": next(
                (item for item in ranked if item.get("cohort_rank") == 1), None,
            ),
        })

    rows.sort(key=lambda item: (
        str(item.get("cohort_id") or ""),
        item.get("cohort_rank") is None,
        int(item.get("cohort_rank") or 0),
    ))
    cohort_rows.sort(key=lambda item: (
        -int(item.get("model_count") or 0),
        str(item.get("research_target") or ""),
        int(item.get("label_horizon_trading_days") or 0),
    ))
    return {
        "schema_version": "alphablocks.model-leaderboard.v1",
        "selection_split": "validation",
        "ranking_metric": "validation.rank_ic",
        "test_metrics_role": "report_only",
        "cohort_definition": [
            "research_target", "prediction_scope", "universe_id", "label_kind",
            "label_horizon_trading_days", "date_start", "date_end",
        ],
        "summary": {
            "model_count": len(normalized),
            "active_count": len(active),
            "candidate_count": sum(_registry_stage(item) == "candidate" for item in active),
            "validated_count": sum(_registry_stage(item) == "validated" for item in active),
            "default_count": sum(bool((item.get("registry") or {}).get("is_default")) for item in active),
            "archived_count": len(normalized) - len(active),
            "cohort_count": len(cohort_rows),
            "validation_gate_passed_count": sum(
                bool(item["validation_gate_passed"]) for item in rows
            ),
        },
        "cohorts": cohort_rows,
        "models": rows,
        "guard": (
            "只在研究目标、股票池、标签、标签周期和日期范围完全一致的队列内，"
            "按验证集RankIC排序；测试集与TopN回测仅用于最终审计。"
        ),
    }


def build_model_research_report(model: Mapping[str, Any]) -> dict[str, Any]:
    source = dict(model)
    spec = dict(source.get("dataset_spec") or {})
    label = dict(spec.get("label") or {})
    split = dict(spec.get("split") or {})
    config = dict(source.get("job_config_json") or {})
    model_config = dict(config.get("model") or {})
    manifest = dict(source.get("manifest_json") or {})
    metrics = dict(source.get("metrics_json") or {})
    validation = dict(source.get("validation") or {})
    registry = dict(source.get("registry") or {})
    backtest = dict(source.get("latest_backtest") or {})
    experiment = dict(source.get("experiment") or {})
    ensemble = dict(manifest.get("ensemble") or {})
    stage = str(registry.get("stage") or source.get("state") or "candidate")
    approved = validation.get("approved") is True
    warnings: list[str] = []
    if stage == "archived":
        warnings.append("模型已归档，不参与新推理、正式信号或回测。")
    if not approved:
        warnings.append("模型尚未通过研究门槛，只能作为研究候选。")
    if validation.get("manual_override") is True:
        warnings.append("模型由人工放行，必须结合放行原因复核。")
    warnings.append("测试集、组合敏感性和TopN回测不参与训练参数或标签周期选择。")

    conclusion = (
        "已归档"
        if stage == "archived"
        else "主模型（已通过研究门槛）"
        if bool(registry.get("is_default")) and approved
        else "已验证，可进入模型池"
        if approved
        else "候选模型，尚不可用于正式策略信号"
    )
    return {
        "schema_version": "alphablocks.model-research-report.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "identity": {
            "model_id": str(source.get("model_id") or ""),
            "model_version": int(source.get("version") or 0),
            "name": str(source.get("name") or ""),
            "model_kind": str(source.get("model_kind") or ""),
            "job_id": str(source.get("job_id") or ""),
            "created_at": source.get("created_at"),
        },
        "executive_summary": {
            "conclusion": conclusion,
            "registry_stage": stage,
            "is_default": bool(registry.get("is_default")),
            "validation_approved": approved,
            "manual_override": bool(validation.get("manual_override")),
            "warnings": warnings,
        },
        "dataset": {
            "dataset_id": str(source.get("dataset_id") or ""),
            "dataset_hash": str(source.get("dataset_hash") or ""),
            "research_target": str(spec.get("research_target") or "stock_selection"),
            "prediction_scope": str(spec.get("prediction_scope") or "stock"),
            "universe_id": str(spec.get("universe_id") or "csi500"),
            "date_start": str(spec.get("date_start") or "")[:10],
            "date_end": str(spec.get("date_end") or "")[:10],
            "data_cutoff": spec.get("data_cutoff"),
            "label_kind": str(label.get("kind") or ""),
            "label_horizon_trading_days": int(label.get("horizon_trading_days") or 5),
            "embargo_days": int(split.get("embargo_days") or label.get("horizon_trading_days") or 5),
            "factor_count": len(spec.get("factors") or []),
            "factors": list(spec.get("factors") or []),
            "materialization": dict(spec.get("materialization") or {}),
            "availability": dict(spec.get("availability") or {}),
        },
        "training": {
            "model": model_config,
            "walk_forward": dict(config.get("walk_forward") or manifest.get("walk_forward") or {}),
            "segments": dict(manifest.get("segments") or {}),
            "environment": dict(manifest.get("environment") or {}),
            "qlib_recorder_id": str(manifest.get("qlib_recorder_id") or ""),
            "qlib_recorder_uri": str(manifest.get("qlib_recorder_uri") or ""),
        },
        "evaluation": {
            "validation": dict(metrics.get("validation") or {}),
            "test": {
                key: metrics.get(key)
                for key in ("rank_ic", "ic", "ic_ir", "rmse", "test_days", "test_rows")
                if metrics.get(key) is not None
            },
            "walk_forward": dict(manifest.get("walk_forward") or {}),
            "validation_gate": validation,
            "backtest": backtest,
        },
        "experiment": experiment,
        "ensemble": ensemble,
        "feature_importance": list(source.get("feature_importance_json") or [])[:50],
        "reproducibility": {
            "dataset_hash": str(source.get("dataset_hash") or ""),
            "dataset_spec_hash": str(manifest.get("dataset_spec_hash") or ""),
            "content_fingerprint": str(manifest.get("content_fingerprint") or ""),
            "inference_run_id": str(
                (source.get("prediction_json") or {}).get("inference_run_id") or ""
            ),
            "future_function_guards": list(manifest.get("future_function_guards") or []),
        },
    }


def render_model_research_report_markdown(report: Mapping[str, Any]) -> str:
    identity = dict(report.get("identity") or {})
    summary = dict(report.get("executive_summary") or {})
    dataset = dict(report.get("dataset") or {})
    training = dict(report.get("training") or {})
    evaluation = dict(report.get("evaluation") or {})
    validation_metrics = dict(evaluation.get("validation") or {})
    test_metrics = dict(evaluation.get("test") or {})
    gate = dict(evaluation.get("validation_gate") or {})
    backtest = dict(evaluation.get("backtest") or {})
    reproducibility = dict(report.get("reproducibility") or {})
    lines = [
        f"# {identity.get('name') or identity.get('model_id')} 研究报告",
        "",
        f"- 模型：`{identity.get('model_id')}` v{identity.get('model_version')}",
        f"- 算法：{identity.get('model_kind') or '--'}",
        f"- 结论：**{summary.get('conclusion') or '--'}**",
        f"- 生成时间：{report.get('generated_at') or '--'}",
        "",
        "## 冻结数据集",
        "",
        f"- Dataset Hash：`{dataset.get('dataset_hash') or '--'}`",
        f"- 研究目标：{dataset.get('research_target') or '--'} / {dataset.get('prediction_scope') or '--'}",
        f"- 股票池：{dataset.get('universe_id') or '--'}",
        f"- 范围：{dataset.get('date_start') or '--'} 至 {dataset.get('date_end') or '--'}",
        f"- 标签：{dataset.get('label_kind') or '--'}（T+{dataset.get('label_horizon_trading_days') or '--'}）",
        f"- 分区隔离：{dataset.get('embargo_days') or '--'} 个交易日",
        f"- 因子数：{dataset.get('factor_count') or 0}",
        "",
        "## 训练与评估",
        "",
        f"- 验证 RankIC：{_format_metric(validation_metrics.get('rank_ic'))}",
        f"- 验证 ICIR：{_format_metric(validation_metrics.get('ic_ir'))}",
        f"- 测试 RankIC：{_format_metric(test_metrics.get('rank_ic') or test_metrics.get('ic'))}",
        f"- 测试 ICIR：{_format_metric(test_metrics.get('ic_ir'))}",
        f"- 研究门槛：{'通过' if gate.get('approved') is True else '未通过'}",
        f"- TopN超额年化：{_format_percent(backtest.get('excess_annual_return'))}",
        f"- TopN夏普：{_format_metric(backtest.get('sharpe_ratio'))}",
        f"- 最大回撤：{_format_percent(backtest.get('max_drawdown'))}",
        "",
        "## 可复现标识",
        "",
        f"- Job ID：`{identity.get('job_id') or '--'}`",
        f"- Recorder ID：`{training.get('qlib_recorder_id') or '--'}`",
        f"- Dataset Spec Hash：`{reproducibility.get('dataset_spec_hash') or '--'}`",
        f"- Content Fingerprint：`{reproducibility.get('content_fingerprint') or '--'}`",
        "",
        "## 风险与约束",
        "",
    ]
    lines.extend(
        f"- {item}" for item in summary.get("warnings") or []
    )
    guards = list(reproducibility.get("future_function_guards") or [])
    if guards:
        lines.extend(["", "### 未来函数防线", ""])
        lines.extend(f"- {item}" for item in guards)
    factors = list(dataset.get("factors") or [])
    if factors:
        lines.extend(["", "## 冻结因子", "", "| 因子 | 版本 | Params Hash | 分类 |", "|---|---:|---|---|"])
        for item in factors:
            lines.append(
                f"| {item.get('label') or item.get('factor_id')} | "
                f"{item.get('factor_version') or '--'} | "
                f"`{item.get('params_hash') or '--'}` | {item.get('category') or '--'} |"
            )
    return "\n".join(lines).rstrip() + "\n"


def _leaderboard_row(model: Mapping[str, Any]) -> dict[str, Any]:
    metrics = dict(model.get("metrics_json") or {})
    validation_metrics = dict(metrics.get("validation") or {})
    validation = dict(model.get("validation") or {})
    backtest = dict(model.get("latest_backtest") or {})
    days = _integer(validation_metrics.get("days"))
    rank_ic = _optional_number(validation_metrics.get("rank_ic"))
    ic_ir = _optional_number(validation_metrics.get("ic_ir"))
    checks = [
        {"key": "days", "threshold": 40, "actual": days, "passed": days >= 40},
        {"key": "rank_ic", "threshold": 0.02, "actual": rank_ic, "passed": rank_ic is not None and rank_ic >= 0.02},
        {"key": "ic_ir", "threshold": 0.3, "actual": ic_ir, "passed": ic_ir is not None and ic_ir >= 0.3},
    ]
    return {
        "model_id": str(model.get("model_id") or ""),
        "model_version": int(model.get("version") or 0),
        "name": str(model.get("name") or ""),
        "model_kind": str(model.get("model_kind") or ""),
        "dataset_hash": str(model.get("dataset_hash") or ""),
        "registry_stage": _registry_stage(model),
        "is_default": bool((model.get("registry") or {}).get("is_default")),
        "validation_rank_ic": rank_ic,
        "validation_ic_ir": ic_ir,
        "validation_days": days,
        "validation_gate_passed": all(item["passed"] for item in checks),
        "validation_gate_checks": checks,
        "test_rank_ic": _optional_number(metrics.get("rank_ic") or metrics.get("ic")),
        "test_ic_ir": _optional_number(metrics.get("ic_ir")),
        "formal_validation_approved": validation.get("approved") is True,
        "manual_override": validation.get("manual_override") is True,
        "backtest": {
            key: backtest.get(key)
            for key in (
                "backtest_job_id", "status", "excess_annual_return",
                "sharpe_ratio", "max_drawdown", "trading_days",
            )
            if backtest.get(key) is not None
        },
        "created_at": model.get("created_at"),
    }


def _registry_stage(model: Mapping[str, Any]) -> str:
    return str(
        (model.get("registry") or {}).get("stage")
        or model.get("state")
        or "candidate"
    )


def _optional_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _number(value: Any, fallback: float = 0.0) -> float:
    result = _optional_number(value)
    return result if result is not None else fallback


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _format_metric(value: Any) -> str:
    number = _optional_number(value)
    return "--" if number is None else f"{number:.4f}"


def _format_percent(value: Any) -> str:
    number = _optional_number(value)
    return "--" if number is None else f"{number * 100:.2f}%"


__all__ = [
    "build_model_leaderboard",
    "build_model_research_report",
    "render_model_research_report_markdown",
]
