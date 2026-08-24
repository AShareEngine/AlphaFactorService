from __future__ import annotations

from factor_service.model_registry import (
    build_model_research_report,
    render_model_research_report_markdown,
)


def _model(
    model_id: str,
    version: int,
    *,
    validation_rank_ic: float,
    validation_ic_ir: float,
    horizon: int = 5,
    stage: str = "candidate",
    approved: bool = False,
    is_default: bool = False,
) -> dict:
    return {
        "model_id": model_id,
        "version": version,
        "job_id": f"job_{model_id}_{version}",
        "dataset_id": f"dataset_{model_id}",
        "dataset_hash": f"hash_{model_id}_{version}",
        "name": f"{model_id} v{version}",
        "model_kind": "lightgbm",
        "state": stage,
        "registry": {
            "stage": stage,
            "is_default": is_default,
            "scope": "stock_selection:csi500",
        },
        "dataset_spec": {
            "research_target": "stock_selection",
            "prediction_scope": "stock",
            "universe_id": "csi500",
            "date_start": "2022-01-01",
            "date_end": "2025-12-31",
            "data_cutoff": "2026-01-01T00:00:00+00:00",
            "label": {
                "kind": f"future_{horizon}d_cross_sectional_rank",
                "horizon_trading_days": horizon,
            },
            "split": {"embargo_days": horizon},
            "materialization": {"mode": "on_demand", "format": "parquet"},
            "availability": {"event_available_at_lte_signal_close": True},
            "factors": [{
                "factor_id": "Price1M", "factor_version": 1,
                "params_hash": "abc123", "category": "动量类因子",
                "label": "一个月价格动量", "params": {},
            }],
        },
        "job_config_json": {
            "model": {"kind": "lightgbm", "params": {"num_leaves": 31}},
            "walk_forward": {"enabled": False, "embargo_days": horizon},
        },
        "metrics_json": {
            "rank_ic": 0.01,
            "ic_ir": 0.05,
            "test_days": 120,
            "validation": {
                "rank_ic": validation_rank_ic,
                "ic_ir": validation_ic_ir,
                "days": 100,
            },
        },
        "validation": {
            "approved": approved,
            "passed": approved,
            "manual_override": False,
            "checks": [],
        },
        "manifest_json": {
            "dataset_spec_hash": f"hash_{model_id}_{version}",
            "content_fingerprint": f"fingerprint_{model_id}_{version}",
            "qlib_recorder_id": f"recorder_{model_id}_{version}",
            "future_function_guards": ["训练特征只读取信号日可用数据"],
        },
        "feature_importance_json": [{"factor": "Price1M", "importance": 1.0}],
        "prediction_json": {"inference_run_id": f"infer_{model_id}_{version}"},
        "latest_backtest": {
            "status": "success",
            "excess_annual_return": 0.03,
            "sharpe_ratio": 0.8,
            "max_drawdown": -0.12,
            "trading_days": 500,
        },
    }


def test_research_report_contains_reproducibility_and_does_not_promote_candidate():
    model = _model("candidate", 2, validation_rank_ic=0.04, validation_ic_ir=0.2)

    report = build_model_research_report(model)
    markdown = render_model_research_report_markdown(report)

    assert report["executive_summary"]["validation_approved"] is False
    assert "候选模型" in report["executive_summary"]["conclusion"]
    assert report["dataset"]["label_horizon_trading_days"] == 5
    assert report["reproducibility"]["content_fingerprint"] == "fingerprint_candidate_2"
    assert "Dataset Hash" in markdown
    assert "训练特征只读取信号日可用数据" in markdown
    assert "测试集、组合敏感性和TopN回测不参与" in markdown


def test_research_report_marks_validated_default_as_primary_model():
    model = _model(
        "champion", 3, validation_rank_ic=0.06, validation_ic_ir=0.5,
        stage="validated", approved=True, is_default=True,
    )

    report = build_model_research_report(model)

    assert report["executive_summary"]["conclusion"] == "主模型（已通过研究门槛）"
    assert report["executive_summary"]["is_default"] is True
