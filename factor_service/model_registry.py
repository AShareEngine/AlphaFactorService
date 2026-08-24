from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Mapping


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


def _optional_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _format_metric(value: Any) -> str:
    number = _optional_number(value)
    return "--" if number is None else f"{number:.4f}"


def _format_percent(value: Any) -> str:
    number = _optional_number(value)
    return "--" if number is None else f"{number * 100:.2f}%"


__all__ = [
    "build_model_research_report",
    "render_model_research_report_markdown",
]
