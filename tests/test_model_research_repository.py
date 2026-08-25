from __future__ import annotations

from datetime import datetime, timezone

import pytest

from factor_service.model_research_repository import (
    ModelResearchRepository,
    ModelResearchConflict,
    ModelResearchError,
    _architecture_readiness,
    _architecture_spec,
    _dataset_spec,
    _ensemble_spec,
    _experiment_summary,
    _factor_ablation_trials,
    _grid_search_trials,
    _horizon_search_values,
    _incremental_training_assessment,
    _job_row,
    _model_spec,
    _model_payload_references,
    _research_origin_spec,
    _research_template_spec,
    _walk_forward_spec,
)


def _source() -> dict:
    return {
        "name": "smoke",
        "date_start": "2020-01-01",
        "date_end": "2024-01-01",
        "data_cutoff": "2024-01-01T15:00:00+08:00",
        "factors": [{
            "factor_id": "mom_20",
            "factor_version": 2,
            "params_hash": "a" * 64,
            "params": {"window": 20},
        }],
    }


def _trained_model(
    model_id: str, version: int, *, validation_icir: float,
    validation_rank_ic: float = 0.03, test_icir: float = 99.0,
    horizon: int = 5, model_kind: str | None = None,
) -> dict:
    return {
        "model_id": model_id,
        "version": version,
        "name": model_id,
        "model_kind": model_kind or ("lightgbm" if version == 1 else "xgboost"),
        "dataset_hash": (model_id[-1:] or "a") * 64,
        "dataset_spec": {
            **_dataset_spec({
                **_source(), "label_horizon_trading_days": horizon,
            }),
            "date_start": "2020-01-01",
            "date_end": "2024-01-01",
        },
        "metrics_json": {
            "ic_ir": test_icir,
            "validation": {
                "days": 80,
                "rank_ic": validation_rank_ic,
                "ic_ir": validation_icir,
            },
        },
        "job_config_json": {"model": {"params": {"seed": 42}}},
    }


def test_model_reference_detection_covers_ensemble_and_incremental_lineage() -> None:
    ensemble = {
        "manifest_json": {
            "ensemble": {
                "sources": [{"model_id": "source-model", "model_version": 2}],
            },
        },
        "config_json": {},
    }
    incremental = {
        "manifest_json": {},
        "config_json": {
            "incremental_training": {
                "source_model_id": "source-model",
                "source_model_version": 2,
            },
        },
    }

    assert _model_payload_references(
        ensemble, model_id="source-model", model_version=2,
    ) is True
    assert _model_payload_references(
        incremental, model_id="source-model", model_version=2,
    ) is True
    assert _model_payload_references(
        ensemble, model_id="source-model", model_version=3,
    ) is False


class _Cursor:
    def __init__(self, row=None) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class _RecordingConnection:
    def __init__(self, job_row: dict, *, existing_version=None) -> None:
        self.job_row = job_row
        self.existing_version = existing_version
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def transaction(self):
        return self

    def execute(self, query, _params=()):
        normalized = " ".join(str(query).split())
        self.queries.append(normalized)
        if normalized.startswith("SELECT * FROM model_jobs"):
            return _Cursor(self.job_row)
        if normalized.startswith("SELECT job_id FROM model_versions"):
            return _Cursor(self.existing_version)
        return _Cursor()


class _RecordingDatabase:
    def __init__(self, connection: _RecordingConnection) -> None:
        self.recording_connection = connection

    def connection(self):
        return self.recording_connection


def test_dataset_contract_locks_versions_and_lookahead_guards() -> None:
    spec = _dataset_spec(_source())

    assert spec["universe_id"] == "csi500"
    assert spec["factors"][0]["factor_version"] == 2
    assert spec["split"]["embargo_days"] == 5
    assert spec["availability"]["event_available_at_lte_signal_close"] is True
    assert spec["availability"]["source_available_at_lte_data_cutoff"] is True
    assert spec["pipeline_version"] == "alphablocks.dataset-pipeline.v3"
    assert spec["sample_filters"] == {
        "minimum_listing_trading_days": 60,
        "exclude_st": True,
        "exclude_delisting": True,
    }
    assert spec["materialization"] == {
        "mode": "on_demand", "format": "parquet", "persist_factor_values": False,
    }


def test_dataset_contract_preserves_legacy_unfiltered_replay() -> None:
    spec = _dataset_spec({
        **_source(),
        "pipeline_version": "alphablocks.dataset-pipeline.v2",
    })

    assert spec["sample_filters"] == {
        "minimum_listing_trading_days": 0,
        "exclude_st": False,
        "exclude_delisting": False,
    }


def test_dataset_contract_validates_sample_filters() -> None:
    with pytest.raises(ModelResearchError, match="0至5000"):
        _dataset_spec({
            **_source(),
            "sample_filters": {
                "minimum_listing_trading_days": 5001,
                "exclude_st": True,
                "exclude_delisting": True,
            },
        })
    with pytest.raises(ModelResearchError, match="exclude_st必须是布尔值"):
        _dataset_spec({
            **_source(),
            "sample_filters": {
                "minimum_listing_trading_days": 60,
                "exclude_st": "yes",
                "exclude_delisting": True,
            },
        })


def test_classification_target_freezes_binary_label_and_model_loss() -> None:
    dataset = _dataset_spec({**_source(), "target_mode": "classification"})
    model = _model_spec(
        {"kind": "lightgbm", "params": {"loss": "mse"}},
        target_mode=dataset["target_mode"],
    )

    assert dataset["target_mode"] == "classification"
    assert dataset["label"] == {
        "kind": "future_5d_direction",
        "mode": "classification",
        "horizon_trading_days": 5,
        "range": [0.0, 1.0],
        "classes": [0, 1],
        "positive_class": "future_return_gt_zero",
        "formula": "1[future_return(T,T+5) > 0]",
    }
    assert model["params"]["loss"] == "binary"
    assert model["params"]["objective"] == "binary"
    assert model["params"]["metric"] == "auc"


def test_model_spec_accepts_quantmind_objective_metrics() -> None:
    regression = _model_spec({
        "kind": "lightgbm",
        "params": {"objective": "regression", "metric": "mae"},
    }, target_mode="return")
    classification = _model_spec({
        "kind": "lightgbm",
        "params": {"objective": "binary", "metric": "binary_logloss"},
    }, target_mode="classification")

    assert regression["params"]["metric"] == "mae"
    assert classification["params"]["metric"] == "binary_logloss"

    with pytest.raises(ModelResearchError, match="Metric"):
        _model_spec({
            "kind": "lightgbm",
            "params": {"objective": "binary", "metric": "rmse"},
        }, target_mode="classification")


def test_completed_job_row_exposes_pending_and_registered_states() -> None:
    pending = _job_row({
        "job_id": "job-pending",
        "kind": "train",
        "status": "succeeded",
        "model_version": None,
        "config_json": {"planned_model_version": 3},
    })
    registered = _job_row({**pending, "model_version": 3})
    declined = _job_row({
        **pending,
        "result_json": {
            "metrics": {"ic": 0.03},
            "registration": {"status": "declined"},
        },
    })

    assert pending["planned_model_version"] == 3
    assert pending["registration_status"] == "pending_confirmation"
    assert registered["registration_status"] == "registered"
    assert declined["registration_status"] == "declined"


def test_training_completion_does_not_insert_model_version_before_confirmation() -> None:
    row = {
        "job_id": "job-pending",
        "kind": "train",
        "status": "running",
        "lease_owner": "alpha-factor-service",
        "lease_token": "lease-token",
        "cancel_requested": False,
        "model_id": "model-pending",
        "dataset_id": "dataset-pending",
        "title": "待确认模型",
        "model_kind": "lightgbm",
        "config_json": {"planned_model_version": 3},
    }
    connection = _RecordingConnection(row)
    repository = ModelResearchRepository(_RecordingDatabase(connection))
    repository.get_job = lambda _job_id: {"kind": "train", "status": "running"}

    repository.complete_job(
        "job-pending", lease_token="lease-token",
        result={"metrics": {"ic": 0.03}},
    )

    sql = "\n".join(connection.queries)
    assert "INSERT INTO model_versions" not in sql
    assert "model_version = NULL" in sql
    assert "INSERT INTO model_job_events" in sql


def test_explicit_registration_inserts_model_version_and_links_artifacts() -> None:
    row = {
        "job_id": "job-pending",
        "kind": "train",
        "status": "succeeded",
        "model_version": None,
        "model_id": "model-pending",
        "dataset_id": "dataset-pending",
        "title": "待确认模型",
        "model_kind": "lightgbm",
        "config_json": {"planned_model_version": 3},
        "result_json": {
            "metrics": {"ic": 0.03},
            "feature_importance": [],
            "predictions": {"row_count": 100},
            "manifest": {"schema_version": "test"},
        },
    }
    connection = _RecordingConnection(row)
    repository = ModelResearchRepository(_RecordingDatabase(connection))
    repository.get_job = lambda _job_id: dict(row)

    repository.register_training_result("job-pending")

    sql = "\n".join(connection.queries)
    assert "INSERT INTO model_versions" in sql
    assert "UPDATE model_jobs SET model_version" in sql
    assert "UPDATE model_artifacts SET model_version" in sql


def test_declining_registration_preserves_result_without_creating_version() -> None:
    row = {
        "job_id": "job-pending",
        "kind": "train",
        "status": "succeeded",
        "model_version": None,
        "result_json": {"metrics": {"ic": 0.03}},
    }
    connection = _RecordingConnection(row)
    repository = ModelResearchRepository(_RecordingDatabase(connection))
    repository.get_job = lambda _job_id: {
        **row,
        "registration_status": "declined",
    }

    repository.decline_training_result("job-pending")

    sql = "\n".join(connection.queries)
    assert "UPDATE model_jobs SET result_json" in sql
    assert "INSERT INTO model_versions" not in sql
    assert "job.registration_declined" not in sql
    assert "INSERT INTO model_job_events" in sql


def test_declined_training_result_cannot_be_registered() -> None:
    repository = ModelResearchRepository.__new__(ModelResearchRepository)
    repository.get_job = lambda _job_id: {
        "job_id": "job-declined",
        "kind": "train",
        "status": "succeeded",
        "result_json": {"registration": {"status": "declined"}},
    }

    with pytest.raises(ModelResearchConflict, match="已选择不入库"):
        repository.register_training_result("job-declined")


def test_parameter_experiment_only_registers_validation_selected_job() -> None:
    repository = ModelResearchRepository.__new__(ModelResearchRepository)
    repository.get_job = lambda _job_id: {
        "job_id": "job-runner-up",
        "kind": "train",
        "status": "succeeded",
        "config_json": {"experiment": {"experiment_id": "experiment-1"}},
    }
    repository.get_training_experiment = lambda _experiment_id: {
        "selection": {
            "status": "selected",
            "selected_job_id": "job-winner",
        },
    }

    with pytest.raises(ModelResearchConflict, match="只允许验证集入选版本"):
        repository.register_training_result("job-runner-up")


def test_dataset_contract_freezes_stock_entity_asset_fields_without_factor_definition() -> None:
    spec = _dataset_spec({
        **_source(),
        "factors": [{
            "feature_kind": "entity_field",
            "factor_id": "entity_stock_daily_close_1234abcd",
            "entity_id": "stock",
            "asset_id": "asset_stock_daily_stock_daily_real",
            "asset_name": "股票日线数据",
            "asset_updated_at": "2026-08-12T16:14:50+00:00",
            "provider_node": "stock_daily_real",
            "field": "close",
            "label": "收盘价",
            "data_type": "number",
        }],
    })

    feature = spec["factors"][0]
    assert feature["feature_kind"] == "entity_field"
    assert feature["asset_id"] == "asset_stock_daily_stock_daily_real"
    assert feature["field"] == "close"
    assert feature["factor_version"] == 1
    assert len(feature["params_hash"]) == 64
    assert feature["params"] == {}


def test_dataset_spec_accepts_custom_split_ratios() -> None:
    spec = _dataset_spec({
        **_source(),
        "split": {"valid": 0.15, "test": 0.1},
    })

    assert spec["split"]["train"] == pytest.approx(0.75)
    assert spec["split"]["valid"] == pytest.approx(0.15)
    assert spec["split"]["test"] == pytest.approx(0.1)
    assert spec["split"]["embargo_days"] == 5


def test_dataset_spec_accepts_custom_universe() -> None:
    spec = _dataset_spec({**_source(), "universe_id": "all_a"})

    assert spec["universe_id"] == "all_a"
    assert spec["index_code"] == "000985.SH"
    assert spec["benchmark_code"] == "000985.SH"

    csi300 = _dataset_spec({**_source(), "universe_id": "csi300"})
    assert csi300["universe_id"] == "csi300"
    assert csi300["index_code"] == "000300.SH"


def test_dataset_spec_rejects_unknown_universe() -> None:
    with pytest.raises(ModelResearchError, match="不支持的股票池"):
        _dataset_spec({**_source(), "universe_id": "csi2000"})


def test_dataset_spec_rejects_invalid_split_ratios() -> None:
    with pytest.raises(ModelResearchError, match="训练集不低于30%"):
        _dataset_spec({**_source(), "split": {"valid": 0.5, "test": 0.4}})
    with pytest.raises(ModelResearchError, match="不低于5%"):
        _dataset_spec({**_source(), "split": {"valid": 0.0, "test": 0.1}})
    with pytest.raises(ModelResearchError, match="必须是数字"):
        _dataset_spec({**_source(), "split": {"valid": "high", "test": 0.2}})


def test_research_template_freezes_complete_single_training_contract() -> None:
    spec = _research_template_spec({
        "name": "LGBM 基线",
        "description": "固定因子和训练边界",
        "training": {
            "title": "中证500基线",
            "model_id": "stock-ranker",
            "dataset": _source(),
            "model": {"kind": "lightgbm", "params": {"num_leaves": 31}},
            "walk_forward": {"enabled": True, "strategy": "expanding"},
            "research_design": {"mode": "single"},
        },
    })

    assert spec["schema_version"] == "alphablocks.research-template.v1"
    training = spec["training"]
    assert training["dataset"]["factors"][0]["params_hash"] == "a" * 64
    assert training["dataset"]["materialization"]["format"] == "parquet"
    assert training["model"]["params"]["seed"] == 42
    assert training["walk_forward"]["strategy"] == "expanding"
    assert training["research_design"] == {"mode": "single", "search": {}}


def test_research_template_validates_and_normalizes_research_design() -> None:
    spec = _research_template_spec({
        "name": "参数研究",
        "training": {
            "dataset": _source(),
            "model": {"kind": "lightgbm", "params": {}},
            "research_design": {
                "mode": "grid",
                "search": {
                    "parameters": {
                        "num_leaves": [15, 31],
                        "learning_rate": [0.03, 0.05],
                    },
                    "max_trials": 6,
                },
            },
        },
    })

    design = spec["training"]["research_design"]
    assert design["mode"] == "grid"
    assert list(design["search"]["parameters"]) == [
        "learning_rate", "num_leaves",
    ]
    assert design["search"]["max_trials"] == 6

    with pytest.raises(ModelResearchError, match="mode只支持"):
        _research_template_spec({
            "name": "非法模板",
            "training": {
                "dataset": _source(),
                "model": {"kind": "lightgbm", "params": {}},
                "research_design": {"mode": "bayesian"},
            },
        })


def test_incremental_training_requires_exact_lightgbm_feature_contract() -> None:
    source = _trained_model(
        "stock-model", 1, validation_icir=0.5, model_kind="lightgbm",
    )
    source.update({
        "job_id": "model_job_source",
        "state": "validated",
        "dataset_hash": "d" * 64,
    })
    candidate_dataset = _dataset_spec({
        **_source(),
        "date_end": "2025-01-02",
        "data_cutoff": "2025-01-02T15:00:00+08:00",
    })
    candidate_model = _model_spec({"kind": "lightgbm", "params": {}})
    bundle = {
        "artifact_id": "artifact-bundle",
        "relative_path": "jobs/source/bundle.tar.gz",
        "sha256": "b" * 64,
        "file_name": "bundle.tar.gz",
    }

    ready = _incremental_training_assessment(
        source, bundle,
        dataset=candidate_dataset,
        model=candidate_model,
        walk_forward=_walk_forward_spec({}),
    )

    assert ready["passed"] is True
    assert ready["contract"]["mode"] == "lightgbm_append_trees_new_data_only"
    assert ready["contract"]["minimum_new_trading_sessions"] == 60

    changed = {
        **candidate_dataset,
        "factors": [{
            **candidate_dataset["factors"][0],
            "params_hash": "c" * 64,
        }],
    }
    blocked = _incremental_training_assessment(
        source, bundle,
        dataset=changed,
        model=candidate_model,
        walk_forward=_walk_forward_spec({}),
    )
    assert blocked["passed"] is False
    assert "feature_identity" in blocked["failed_checks"]


def test_dataset_contract_freezes_label_horizon_and_matching_embargo() -> None:
    spec = _dataset_spec({
        **_source(),
        "label_horizon_trading_days": 10,
    })

    assert spec["label"] == {
        "kind": "future_10d_cross_sectional_rank",
        "horizon_trading_days": 10,
        "range": [-1.0, 1.0],
    }
    assert spec["split"]["embargo_days"] == 10


@pytest.mark.parametrize("horizon", [0, 31, "invalid"])
def test_dataset_contract_rejects_unsupported_label_horizon(horizon) -> None:
    with pytest.raises(ModelResearchError, match=r"T\+1至T\+30"):
        _dataset_spec({
            **_source(),
            "label_horizon_trading_days": horizon,
        })


@pytest.mark.parametrize("horizon", [1, 2, 5, 20, 30])
def test_dataset_contract_accepts_single_horizon_range(horizon) -> None:
    spec = _dataset_spec({
        **_source(),
        "label_horizon_trading_days": horizon,
    })
    assert spec["label"]["horizon_trading_days"] == horizon
    assert spec["split"]["embargo_days"] == horizon


def test_training_job_freezes_remote_execution_node() -> None:
    # The normalizer is exercised separately from database writes through the
    # exported job spec helpers used by repository tests.
    from factor_service.model_research_repository import _execution_spec

    assert _execution_spec({"node_id": "autodl-gpu-01"}) == {
        "node_id": "autodl-gpu-01",
        "mode": "remote_ssh_docker",
    }
    assert _execution_spec({}) == {"node_id": "local", "mode": "local"}


def test_market_style_dataset_contract_freezes_target_and_style_label() -> None:
    source = {**_source(), "research_target": "market_style"}

    spec = _dataset_spec(source)

    assert spec["research_target"] == "market_style"
    assert spec["prediction_scope"] == "market_style"
    assert spec["label"] == {
        "kind": "future_5d_market_style_rank",
        "horizon_trading_days": 5,
        "range": [-1.0, 1.0],
        "entities": ["STYLE_SMALL", "STYLE_LARGE"],
    }


def test_industry_rotation_dataset_rejects_pre_sw2021_history() -> None:
    source = {**_source(), "research_target": "industry_rotation"}

    with pytest.raises(ModelResearchError, match="2021-12-13"):
        _dataset_spec(source)


def test_industry_rotation_dataset_freezes_sw2021_daily_snapshot_contract() -> None:
    source = {
        **_source(),
        "research_target": "industry_rotation",
        "date_start": "2022-01-04",
    }

    spec = _dataset_spec(source)

    assert spec["research_target"] == "industry_rotation"
    assert spec["prediction_scope"] == "industry"
    assert spec["label"] == {
        "kind": "future_5d_industry_rank",
        "horizon_trading_days": 5,
        "range": [-1.0, 1.0],
        "classification": "sw2021_l1",
        "safe_start": "2021-12-13",
    }
    assert spec["availability"]["industry_snapshot_date_eq_signal_date"] is True


def test_dataset_contract_rejects_unlocked_factor() -> None:
    source = _source()
    del source["factors"][0]["params_hash"]

    with pytest.raises(ModelResearchError, match="params_hash"):
        _dataset_spec(source)


def _origin_source_job() -> dict:
    dataset = _dataset_spec(_source())
    model = _model_spec({
        "kind": "lightgbm",
        "params": {"learning_rate": 0.03, "num_leaves": 15},
    })
    walk_forward = _walk_forward_spec({
        "enabled": True,
        "train_years": 1,
        "valid_months": 3,
        "test_months": 12,
        "step_months": 12,
        "max_windows": 3,
        "embargo_days": 5,
    })
    return {
        "job_id": "source-job",
        "status": "succeeded",
        "model_id": "source-model",
        "model_version": 2,
        "dataset_hash": "f" * 64,
        "dataset_spec": dataset,
        "config_json": {"model": model, "walk_forward": walk_forward},
    }


def test_research_origin_marks_identical_configuration_as_exact_replay() -> None:
    source_job = _origin_source_job()
    result = _research_origin_spec(
        {"requested_mode": "exact_replay"},
        source_type="model_version",
        source_id="source-model.v2",
        source_job=source_job,
        source_model_id="source-model",
        source_model_version=2,
        dataset=source_job["dataset_spec"],
        model=source_job["config_json"]["model"],
        walk_forward=source_job["config_json"]["walk_forward"],
    )

    assert result["mode"] == "exact_replay"
    assert result["changed_sections"] == []
    assert result["source_dataset_hash"] == "f" * 64
    assert len(result["source_config_hash"]) == 64


def test_research_origin_rejects_changed_configuration_claiming_exact_replay() -> None:
    source_job = _origin_source_job()
    changed_model = _model_spec({
        "kind": "lightgbm",
        "params": {"learning_rate": 0.08, "num_leaves": 15},
    })

    with pytest.raises(ModelResearchConflict, match="model"):
        _research_origin_spec(
            {"requested_mode": "exact_replay"},
            source_type="experiment",
            source_id="experiment-a",
            source_job=source_job,
            source_model_id="source-model",
            source_model_version=2,
            dataset=source_job["dataset_spec"],
            model=changed_model,
            walk_forward=source_job["config_json"]["walk_forward"],
        )


def test_research_origin_records_derived_study_and_declared_design_change() -> None:
    source_job = _origin_source_job()
    result = _research_origin_spec(
        {
            "requested_mode": "derived",
            "declared_changes": ["research_design"],
        },
        source_type="model_version",
        source_id="source-model.v2",
        source_job=source_job,
        source_model_id="source-model",
        source_model_version=2,
        dataset=source_job["dataset_spec"],
        model=source_job["config_json"]["model"],
        walk_forward=source_job["config_json"]["walk_forward"],
    )

    assert result["mode"] == "derived"
    assert result["changed_sections"] == ["research_design"]


def test_research_origin_resolver_verifies_model_version_owns_source_job() -> None:
    source_job = _origin_source_job()
    repository = ModelResearchRepository.__new__(ModelResearchRepository)
    repository.get_model = lambda model_id, version: {
        "model_id": model_id,
        "version": version,
        "job_id": "different-job",
    }

    with pytest.raises(ModelResearchConflict, match="模型版本与来源训练任务不匹配"):
        repository._resolve_research_origin(
            {
                "source_type": "model_version",
                "source_id": "source-model.v2",
                "source_job_id": source_job["job_id"],
                "source_model_id": "source-model",
                "source_model_version": 2,
                "requested_mode": "exact_replay",
            },
            dataset=source_job["dataset_spec"],
            model=source_job["config_json"]["model"],
            walk_forward=source_job["config_json"]["walk_forward"],
        )


def test_research_origin_resolver_only_accepts_selected_experiment_job() -> None:
    source_job = _origin_source_job()
    repository = ModelResearchRepository.__new__(ModelResearchRepository)
    repository.get_training_experiment = lambda _experiment_id: {
        "selection": {
            "status": "selected",
            "selected_job_id": "another-job",
            "selected_model_id": "source-model",
            "selected_model_version": 2,
        },
    }

    with pytest.raises(ModelResearchConflict, match="验证集选出的入选任务"):
        repository._resolve_research_origin(
            {
                "source_type": "experiment",
                "source_id": "experiment-a",
                "source_job_id": source_job["job_id"],
                "requested_mode": "exact_replay",
            },
            dataset=source_job["dataset_spec"],
            model=source_job["config_json"]["model"],
            walk_forward=source_job["config_json"]["walk_forward"],
        )


def test_ensemble_contract_freezes_sources_and_equal_weights() -> None:
    sources = [_trained_model("model-a", 1, validation_icir=0.4), _trained_model("model-b", 2, validation_icir=0.8)]

    spec = _ensemble_spec({
        "name": "LGBM + XGB",
        "sources": [
            {"model_id": "model-a", "model_version": 1},
            {"model_id": "model-b", "model_version": 2},
        ],
        "weight_strategy": "equal",
    }, sources)

    assert spec["fusion_method"] == "linear_score"
    assert [item["weight"] for item in spec["sources"]] == [0.5, 0.5]
    assert spec["dataset"]["materialization"]["mode"] == "model_prediction_fusion"
    assert spec["dataset"]["ensemble_sources"][0]["dataset_hash"] == sources[0]["dataset_hash"]


def test_ensemble_icir_weights_only_use_validation_metrics() -> None:
    sources = [
        _trained_model("model-a", 1, validation_icir=0.2, test_icir=999),
        _trained_model("model-b", 2, validation_icir=0.8, test_icir=-999),
    ]

    spec = _ensemble_spec({
        "sources": [
            {"model_id": "model-a", "model_version": 1},
            {"model_id": "model-b", "model_version": 2},
        ],
        "weight_strategy": "validation_icir",
    }, sources)

    assert [item["weight"] for item in spec["sources"]] == pytest.approx([0.2, 0.8])
    assert spec["weight_metric"] == "validation.ic_ir"


def test_multi_horizon_ensemble_freezes_primary_evaluation_horizon() -> None:
    sources = [
        _trained_model(
            "horizon-a", 1, validation_icir=0.2, horizon=1,
            model_kind="lightgbm",
        ),
        _trained_model(
            "horizon-b", 2, validation_icir=0.8, horizon=5,
            model_kind="lightgbm",
        ),
    ]

    spec = _ensemble_spec({
        "ensemble_mode": "multi_horizon",
        "evaluation_horizon_trading_days": 5,
        "weight_strategy": "validation_icir",
        "sources": [
            {"model_id": "horizon-a", "model_version": 1},
            {"model_id": "horizon-b", "model_version": 2},
        ],
    }, sources)

    assert spec["ensemble_mode"] == "multi_horizon"
    assert spec["source_horizons"] == [1, 5]
    assert spec["evaluation_horizon_trading_days"] == 5
    assert spec["dataset"]["label"]["horizon_trading_days"] == 5
    assert spec["dataset"]["split"]["embargo_days"] == 5
    assert spec["dataset"]["materialization"]["mode"] == (
        "multi_horizon_model_prediction_fusion"
    )
    assert [item["weight"] for item in spec["sources"]] == pytest.approx([0.2, 0.8])


def test_multi_horizon_ensemble_rejects_different_factors() -> None:
    first = _trained_model(
        "horizon-a", 1, validation_icir=0.2, horizon=1,
        model_kind="lightgbm",
    )
    second = _trained_model(
        "horizon-b", 2, validation_icir=0.8, horizon=5,
        model_kind="lightgbm",
    )
    second["dataset_spec"] = {
        **second["dataset_spec"],
        "factors": [{
            **second["dataset_spec"]["factors"][0],
            "factor_id": "different_factor",
        }],
    }

    with pytest.raises(ModelResearchError, match="完全相同的冻结因子"):
        _ensemble_spec({
            "ensemble_mode": "multi_horizon",
            "sources": [
                {"model_id": "horizon-a", "model_version": 1},
                {"model_id": "horizon-b", "model_version": 2},
            ],
        }, [first, second])


def test_ensemble_rejects_duplicate_source_or_nonpositive_manual_weight() -> None:
    source = _trained_model("model-a", 1, validation_icir=0.4)
    with pytest.raises(ModelResearchError, match="重复"):
        _ensemble_spec({"sources": [
            {"model_id": "model-a", "model_version": 1},
            {"model_id": "model-a", "model_version": 1},
        ]}, [source, source])
    with pytest.raises(ModelResearchError, match="大于0"):
        _ensemble_spec({
            "weight_strategy": "manual",
            "sources": [
                {"model_id": "model-a", "model_version": 1, "weight": 1},
                {"model_id": "model-b", "model_version": 2, "weight": 0},
            ],
        }, [source, _trained_model("model-b", 2, validation_icir=0.5)])


def test_model_architecture_locks_engine_models_and_frozen_feature_groups() -> None:
    models = [
        {
            **_trained_model("model-a", 1, validation_icir=0.4),
            "state": "validated",
            "prediction_json": {
                "row_count": 1000, "date_start": "2024-01-02",
                "date_end": "2024-06-28",
            },
            "manifest_json": {"model_params": {"num_leaves": 64}},
        },
        {
            **_trained_model("model-b", 2, validation_icir=0.6),
            "state": "validated",
            "prediction_json": {
                "row_count": 1000, "date_start": "2024-03-01",
                "date_end": "2024-09-30",
            },
            "manifest_json": {"model_params": {"max_depth": 6}},
        },
    ]
    spec = _architecture_spec({
        "name": "大小盘双引擎",
        "merge_method": "weighted_score",
        "top_n": 20,
        "rebalance_every": 5,
        "engines": [
            {
                "engine_key": "large_cap", "display_name": "大盘引擎",
                "role": "large_cap_selection", "model_id": "model-a",
                "model_version": 1, "weight": 2, "score_threshold": 0.4,
                "priority": 1,
            },
            {
                "engine_key": "small_cap", "display_name": "小盘引擎",
                "role": "small_cap_selection", "model_id": "model-b",
                "model_version": 2, "weight": 1, "score_threshold": 0.6,
                "priority": 2,
            },
        ],
    }, models)

    assert spec["schema_version"] == "alphablocks.model-architecture.v2"
    assert spec["pipeline_mode"] == "flat"
    assert spec["universe_id"] == "csi500"
    assert spec["engine_count"] == 2
    assert spec["engines"][0]["factor_ids"] == ["mom_20"]
    assert spec["engines"][0]["architecture"] == {"num_leaves": 64}
    assert [item["normalized_weight"] for item in spec["engines"]] == pytest.approx([
        2 / 3, 1 / 3,
    ])
    assert spec["execution_contract"]["engine_features"] == "locked_by_model_version"

    readiness = _architecture_readiness(spec, models)
    assert readiness["ready"] is True
    assert readiness["definition_only"] is False
    assert readiness["research_backtest_ready"] is True
    common = next(
        item for item in readiness["checks"]
        if item["key"] == "common_prediction_range"
    )
    assert common["actual"] == "2024-03-01 至 2024-06-28"

    no_overlap = _architecture_readiness(spec, [
        models[0],
        {**models[1], "prediction_json": {
            "row_count": 1000, "date_start": "2025-01-02",
            "date_end": "2025-06-30",
        }},
    ])
    assert no_overlap["ready"] is False
    assert no_overlap["research_backtest_ready"] is False
    assert "common_prediction_range" in no_overlap["failed_checks"]


def test_hierarchical_architecture_freezes_complete_decision_chain() -> None:
    models = [
        {
            **_trained_model(model_id, version, validation_icir=0.4),
            "state": "validated",
            "prediction_json": {
                "row_count": 1000, "date_start": "2024-01-02",
                "date_end": "2024-06-28",
            },
        }
        for model_id, version in (
            ("model-style", 1), ("model-industry", 2), ("model-stock", 3),
        )
    ]
    models[0]["dataset_spec"] = {
        **models[0]["dataset_spec"],
        "research_target": "market_style",
        "prediction_scope": "market_style",
    }
    # 行业训练当前会被真实能力检查阻断；这里构造未来具备PIT时间戳后
    # 注册的不可变模型，用于验证三级架构的执行契约。
    models[1]["dataset_spec"] = {
        **models[1]["dataset_spec"],
        "research_target": "industry_rotation",
        "prediction_scope": "industry",
    }
    spec = _architecture_spec({
        "name": "风格行业个股三级架构",
        "pipeline_mode": "hierarchical",
        "merge_method": "weighted_score",
        "engines": [
            {
                "engine_key": "style", "role": "market_style",
                "model_id": "model-style", "model_version": 1,
            },
            {
                "engine_key": "industry", "role": "industry_rotation",
                "model_id": "model-industry", "model_version": 2,
            },
            {
                "engine_key": "stock", "role": "stock_selection",
                "model_id": "model-stock", "model_version": 3,
            },
        ],
    }, models)

    assert spec["pipeline_mode"] == "hierarchical"
    assert [item["stage"] for item in spec["engines"]] == [
        "style_gate", "industry_gate", "stock_rank",
    ]
    assert spec["execution_contract"]["stage_order"] == [
        "style_gate", "industry_gate", "risk_gate", "stock_rank",
    ]
    readiness = _architecture_readiness(spec, models)
    assert readiness["research_backtest_ready"] is True
    assert readiness["stage_counts"] == {
        "style_gate": 1, "industry_gate": 1, "stock_rank": 1,
    }


def test_hierarchical_architecture_rejects_missing_industry_stage() -> None:
    models = [
        _trained_model("model-style", 1, validation_icir=0.4),
        _trained_model("model-stock", 2, validation_icir=0.4),
    ]
    models[0]["dataset_spec"] = {
        **models[0]["dataset_spec"],
        "research_target": "market_style",
        "prediction_scope": "market_style",
    }
    with pytest.raises(ModelResearchError, match="行业轮动"):
        _architecture_spec({
            "name": "不完整三级架构",
            "pipeline_mode": "hierarchical",
            "merge_method": "weighted_score",
            "engines": [
                {
                    "role": "market_style", "model_id": "model-style",
                    "model_version": 1,
                },
                {
                    "role": "stock_selection", "model_id": "model-stock",
                    "model_version": 2,
                },
            ],
        }, models)


def test_model_architecture_rejects_duplicate_priority_and_unvalidated_activation() -> None:
    models = [
        {**_trained_model("model-a", 1, validation_icir=0.4), "state": "validated"},
        {**_trained_model("model-b", 2, validation_icir=0.6), "state": "candidate"},
    ]
    with pytest.raises(ModelResearchError, match="不同优先级"):
        _architecture_spec({
            "name": "冲突架构", "merge_method": "priority",
            "engines": [
                {"model_id": "model-a", "model_version": 1, "priority": 1},
                {"model_id": "model-b", "model_version": 2, "priority": 1},
            ],
        }, models)

    spec = _architecture_spec({
        "name": "候选架构", "merge_method": "union",
        "engines": [
            {"model_id": "model-a", "model_version": 1, "priority": 1},
            {"model_id": "model-b", "model_version": 2, "priority": 2},
        ],
    }, models)
    readiness = _architecture_readiness(spec, [
        {**models[0], "prediction_json": {
            "row_count": 100, "date_start": "2024-01-02",
            "date_end": "2024-06-28",
        }},
        {**models[1], "prediction_json": {
            "row_count": 100, "date_start": "2024-03-01",
            "date_end": "2024-09-30",
        }},
    ])
    assert readiness["ready"] is False
    assert readiness["research_backtest_ready"] is True
    assert "models_validated" in readiness["failed_checks"]
    assert readiness["research_failed_checks"] == []


def test_model_architecture_freezes_aligned_walk_forward_windows() -> None:
    windows = [
        {"window": 1, "segments": {"test": ["2021-05-11", "2022-05-24"]},
         "metrics": {"rank_ic": 0.03, "ic_ir": 0.4, "test_days": 252}},
        {"window": 2, "segments": {"test": ["2022-05-25", "2023-06-05"]},
         "metrics": {"rank_ic": 0.04, "ic_ir": 0.5, "test_days": 252}},
        {"window": 3, "segments": {"test": ["2023-06-06", "2024-06-20"]},
         "metrics": {"rank_ic": 0.05, "ic_ir": 0.6, "test_days": 252}},
    ]
    models = [
        {
            **_trained_model(model_id, version, validation_icir=0.4),
            "state": "candidate",
            "manifest_json": {"walk_forward": {
                "enabled": True, "strategy": "rolling", "windows": windows,
            }},
        }
        for model_id, version in [("model-a", 1), ("model-b", 2)]
    ]

    spec = _architecture_spec({
        "name": "WFA双引擎", "merge_method": "weighted_score",
        "engines": [
            {"model_id": "model-a", "model_version": 1, "priority": 1},
            {"model_id": "model-b", "model_version": 2, "priority": 2},
        ],
    }, models)

    assert spec["walk_forward"]["eligible"] is True
    assert spec["walk_forward"]["window_count"] == 3
    assert spec["engines"][0]["walk_forward"]["windows"][0] == {
        "window": 1,
        "test_start": "2021-05-11",
        "test_end": "2022-05-24",
        "rank_ic": 0.03,
        "ic_ir": 0.4,
        "test_days": 252,
    }


def test_model_architecture_rejects_misaligned_walk_forward_contract() -> None:
    models = [
        {
            **_trained_model("model-a", 1, validation_icir=0.4),
            "manifest_json": {"walk_forward": {
                "enabled": True, "windows": [{
                    "window": 1,
                    "segments": {"test": ["2021-01-01", "2021-12-31"]},
                }],
            }},
        },
        {
            **_trained_model("model-b", 2, validation_icir=0.4),
            "manifest_json": {"walk_forward": {
                "enabled": True, "windows": [{
                    "window": 1,
                    "segments": {"test": ["2022-01-01", "2022-12-31"]},
                }],
            }},
        },
    ]
    spec = _architecture_spec({
        "name": "错位WFA", "engines": [
            {"model_id": "model-a", "model_version": 1, "priority": 1},
            {"model_id": "model-b", "model_version": 2, "priority": 2},
        ],
    }, models)

    assert spec["walk_forward"]["eligible"] is False
    assert "窗口不一致" in spec["walk_forward"]["reason"]


def test_model_contract_uses_whitelist_and_deterministic_seed() -> None:
    spec = _model_spec({"params": {"learning_rate": 0.03, "unsafe": "ignored"}})

    assert spec["params"]["learning_rate"] == 0.03
    assert spec["params"]["seed"] == 42
    assert "unsafe" not in spec["params"]


def test_all_supported_models_have_distinct_training_implementations() -> None:
    expected = {
        "lightgbm": "qlib.contrib.model.gbdt.LGBModel",
        "xgboost": "qlib.contrib.model.xgboost.XGBModel",
        "catboost": "qlib.contrib.model.catboost_model.CatBoostModel",
        "mlp": "factor_service.research.models.QlibTorchMLPModel",
        "lstm": "factor_service.research.models.QlibTorchLSTMModel",
        "transformer_lstm": "factor_service.research.models.QlibTorchTransformerLSTMModel",
    }

    for kind, implementation in expected.items():
        spec = _model_spec({"kind": kind, "params": {}})
        assert spec["qlib_model"] == implementation
        assert spec["params"]["seed"] == 42


def test_mlp_contract_freezes_explicit_hidden_layer_widths() -> None:
    spec = _model_spec({
        "kind": "mlp",
        "params": {"hidden_layers": [64, 128, 256]},
    })

    assert spec["params"]["hidden_layers"] == [64, 128, 256]
    assert "hidden_size" not in spec["params"]
    assert "layer_count" not in spec["params"]


def test_mlp_contract_rejects_invalid_hidden_layer_widths() -> None:
    with pytest.raises(ModelResearchError, match="hidden_layers"):
        _model_spec({"kind": "mlp", "params": {"hidden_layers": [64, 5000]}})


def test_lstm_contract_freezes_sequence_architecture() -> None:
    spec = _model_spec({
        "kind": "lstm",
        "params": {"lookback_window": 40, "hidden_size": 96, "num_layers": 3},
    })

    assert spec["params"]["lookback_window"] == 40
    assert spec["params"]["hidden_size"] == 96
    assert spec["params"]["num_layers"] == 3
    assert spec["params"]["learning_rate"] == 0.001


def test_transformer_lstm_contract_freezes_both_encoders() -> None:
    spec = _model_spec({
        "kind": "transformer_lstm",
        "params": {
            "lookback_window": 40, "d_model": 48, "nhead": 4,
            "transformer_layers": 2, "lstm_hidden_size": 96,
        },
    })

    assert spec["params"]["lookback_window"] == 40
    assert spec["params"]["d_model"] == 48
    assert spec["params"]["nhead"] == 4
    assert spec["params"]["lstm_hidden_size"] == 96


def test_transformer_lstm_contract_rejects_incompatible_heads() -> None:
    with pytest.raises(ModelResearchError, match="d_model.*nhead"):
        _model_spec({
            "kind": "transformer_lstm", "params": {"d_model": 30, "nhead": 8},
        })


def test_walk_forward_contract_has_strict_independent_test_defaults() -> None:
    spec = _walk_forward_spec({"enabled": True})

    assert spec["strategy"] == "rolling"
    assert spec["valid_months"] == 6
    assert spec["test_months"] == 12
    assert spec["step_months"] == 12
    assert spec["embargo_days"] == 5


def test_walk_forward_contract_rejects_overlapping_test_windows() -> None:
    with pytest.raises(ModelResearchError, match="样本外预测重叠"):
        _walk_forward_spec({"enabled": True, "test_months": 12, "step_months": 6})


def test_job_transport_serializes_database_timestamps() -> None:
    row = {
        "job_id": "model_job_test",
        "lease_token": "secret-token",
        "requested_at": datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
    }

    result = _job_row(row)

    assert "lease_token" not in result
    assert result["requested_at"] == "2026-08-14T12:00:00+00:00"


def test_grid_search_builds_deterministic_cartesian_trials() -> None:
    trials = _grid_search_trials(
        {"kind": "lightgbm", "params": {"n_estimators": 200}},
        {
            "strategy": "grid",
            "parameters": {
                "num_leaves": [31, 63],
                "learning_rate": [0.03, 0.05],
            },
            "max_trials": 4,
        },
    )

    assert len(trials) == 4
    assert [
        (trial["learning_rate"], trial["num_leaves"]) for trial in trials
    ] == [(0.03, 31), (0.03, 63), (0.05, 31), (0.05, 63)]
    assert all(trial["n_estimators"] == 200 for trial in trials)
    assert all(trial["seed"] == 42 for trial in trials)


def test_grid_search_supports_mlp_architecture_candidates() -> None:
    trials = _grid_search_trials(
        {"kind": "mlp", "params": {}},
        {
            "parameters": {
                "hidden_layers": [[64, 128], [64, 128, 256]],
            },
        },
    )

    assert [trial["hidden_layers"] for trial in trials] == [
        [64, 128], [64, 128, 256],
    ]


def test_horizon_search_normalizes_supported_unique_values() -> None:
    assert _horizon_search_values({"horizons": [10, 3, 5, 3]}) == [3, 5, 10]

    with pytest.raises(ModelResearchError, match="至少需要两个不同"):
        _horizon_search_values({"horizons": [5, 5]})
    with pytest.raises(ModelResearchError, match="只支持"):
        _horizon_search_values({"horizons": [1, 20]})


def test_canceled_experiment_restart_clones_frozen_trials_into_new_experiment() -> None:
    repository = object.__new__(ModelResearchRepository)
    source_experiment_id = "model_experiment_failed"
    source_jobs = [
        {
            "job_id": f"failed-job-{index}",
            "model_id": "shared-model",
            "model_kind": kind,
            "title": f"模型对比 · {index}/2 · {kind}",
            "status": status,
            "dataset_hash": "a" * 64,
            "dataset_spec": _source(),
            "requested_at": f"2026-08-25T01:0{index}:00+00:00",
            "updated_at": f"2026-08-25T01:0{index}:00+00:00",
            "config_json": {
                "model": {"kind": kind, "params": {"seed": 42}},
                "walk_forward": {"enabled": True},
                "execution": {"node_id": "autodl-pro-test-01"},
                "experiment": {
                    "experiment_id": source_experiment_id,
                    "title": "模型对比",
                    "strategy": "model_ensemble",
                    "trial_index": index,
                    "trial_count": 2,
                    "search_params": {"model_kind": kind},
                    "auto_dispatch": True,
                },
            },
        }
        for index, (kind, status) in enumerate(
            (("lightgbm", "canceled"), ("lstm", "canceled")), start=1,
        )
    ]
    repository.get_training_experiment = lambda experiment_id: {
        "experiment_id": experiment_id,
        "title": "模型对比",
        "model_id": "shared-model",
        "jobs": source_jobs,
    }
    captured = []

    def create_job(payload):
        captured.append(payload)
        index = len(captured)
        return {
            "job_id": f"retried-job-{index}",
            "model_id": payload["model_id"],
            "model_kind": payload["model"]["kind"],
            "title": payload["title"],
            "status": "queued",
            "dataset_hash": "a" * 64,
            "dataset_spec": payload["dataset"],
            "requested_at": f"2026-08-25T02:0{index}:00+00:00",
            "updated_at": f"2026-08-25T02:0{index}:00+00:00",
            "config_json": {
                "model": payload["model"],
                "execution": payload["execution"],
                "experiment": payload["experiment"],
            },
        }

    repository.create_training_job = create_job

    restarted = repository.restart_training_experiment(source_experiment_id)

    assert restarted["experiment_id"] != source_experiment_id
    assert restarted["restarted_from_experiment_id"] == source_experiment_id
    assert restarted["statuses"] == {"queued": 2}
    assert [item["model"]["kind"] for item in captured] == ["lightgbm", "lstm"]
    assert all(
        item["experiment"]["experiment_id"] == restarted["experiment_id"]
        and item["experiment"]["auto_dispatch"] is True
        for item in captured
    )
    assert all(item["dataset"] == _source() for item in captured)


def test_horizon_experiment_creates_separate_frozen_datasets() -> None:
    repository = object.__new__(ModelResearchRepository)
    captured = []

    def create_job(payload):
        captured.append(payload)
        index = len(captured)
        dataset = payload["dataset"]
        return {
            "job_id": f"horizon-job-{index}",
            "model_id": payload["model_id"],
            "model_kind": payload["model"]["kind"],
            "status": "queued",
            "dataset_hash": str(index) * 64,
            "dataset_spec": dataset,
            "requested_at": f"2026-08-15T01:0{index}:00+00:00",
            "updated_at": f"2026-08-15T01:0{index}:00+00:00",
            "config_json": {"experiment": payload["experiment"]},
        }

    repository.create_training_job = create_job
    result = repository.create_training_experiment({
        "title": "多周期研究",
        "model_id": "horizon-model",
        "dataset": _source(),
        "model": {"kind": "lightgbm", "params": {}},
        "search": {"strategy": "horizon_grid", "horizons": [1, 5, 10]},
    })

    assert result["strategy"] == "horizon_grid"
    assert result["shared_dataset"] is False
    assert result["dataset_count"] == 3
    assert result["label_horizons"] == [1, 5, 10]
    assert result["search_parameters"] == ["label_horizon_trading_days"]
    assert [
        item["dataset"]["label"]["horizon_trading_days"] for item in captured
    ] == [1, 5, 10]
    assert [item["dataset"]["split"]["embargo_days"] for item in captured] == [1, 5, 10]
    assert [item["walk_forward"]["embargo_days"] for item in captured] == [1, 5, 10]
    assert [
        item["experiment"]["search_params"]["label_horizon_trading_days"]
        for item in captured
    ] == [1, 5, 10]


def test_factor_ablation_builds_baseline_and_single_removal_datasets() -> None:
    source = {
        **_source(),
        "factors": [
            {
                "factor_id": factor_id,
                "factor_version": 1,
                "params_hash": factor_id * 8,
                "params": {},
            }
            for factor_id in ("mom", "value", "risk")
        ],
    }

    trials = _factor_ablation_trials(
        source,
        {"factor_ids": ["value", "mom", "value"], "max_trials": 3},
    )

    assert [
        item["search_params"]["removed_factor_id"] for item in trials
    ] == ["__baseline__", "value", "mom"]
    assert [
        [factor["factor_id"] for factor in item["dataset"]["factors"]]
        for item in trials
    ] == [
        ["mom", "value", "risk"],
        ["mom", "risk"],
        ["value", "risk"],
    ]


def test_factor_ablation_rejects_unknown_or_oversized_selection() -> None:
    source = {
        **_source(),
        "factors": [
            {
                "factor_id": "mom", "factor_version": 1,
                "params_hash": "a" * 16, "params": {},
            },
            {
                "factor_id": "risk", "factor_version": 1,
                "params_hash": "b" * 16, "params": {},
            },
        ],
    }

    with pytest.raises(ModelResearchError, match="不存在"):
        _factor_ablation_trials(source, {"factor_ids": ["unknown"]})
    with pytest.raises(ModelResearchError, match="超过本次上限"):
        _factor_ablation_trials(
            source, {"factor_ids": ["mom", "risk"], "max_trials": 2},
        )


def test_factor_ablation_experiment_freezes_variants_and_selection_policy() -> None:
    repository = object.__new__(ModelResearchRepository)
    captured = []
    source = {
        **_source(),
        "factors": [
            {
                "factor_id": factor_id,
                "factor_version": 1,
                "params_hash": (factor_id * 16)[:16],
                "params": {},
            }
            for factor_id in ("mom", "value", "risk")
        ],
    }

    def create_job(payload):
        captured.append(payload)
        index = len(captured)
        return {
            "job_id": f"ablation-job-{index}",
            "model_id": payload["model_id"],
            "model_kind": payload["model"]["kind"],
            "status": "queued",
            "dataset_hash": str(index) * 64,
            "dataset_spec": payload["dataset"],
            "requested_at": f"2026-08-15T02:0{index}:00+00:00",
            "updated_at": f"2026-08-15T02:0{index}:00+00:00",
            "config_json": {"experiment": payload["experiment"]},
        }

    repository.create_training_job = create_job
    result = repository.create_training_experiment({
        "title": "因子消融",
        "model_id": "ablation-model",
        "dataset": source,
        "model": {"kind": "lightgbm", "params": {}},
        "search": {
            "strategy": "factor_ablation",
            "factor_ids": ["value", "mom"],
            "max_trials": 3,
        },
    })

    assert result["strategy"] == "factor_ablation"
    assert result["parent_experiment_id"] == ""
    assert result["iteration"] == 1
    assert result["dataset_count"] == 3
    assert result["shared_dataset"] is False
    assert result["search_parameters"] == ["removed_factor_id"]
    assert result["selection"]["policy"] == "alphablocks.factor-ablation-selection.v1"
    assert [
        item["experiment"]["search_params"]["removed_factor_id"]
        for item in captured
    ] == ["__baseline__", "value", "mom"]
    assert [len(item["dataset"]["factors"]) for item in captured] == [3, 2, 2]
    assert result["lineage_trial_count"] == 3
    assert result["lineage_trial_remaining"] == 21


def test_model_ensemble_experiment_trains_each_selected_kind_once() -> None:
    repository = object.__new__(ModelResearchRepository)
    captured = []

    def create_job(payload):
        captured.append(payload)
        index = len(captured)
        return {
            "job_id": f"ensemble-job-{index}",
            "model_id": payload["model_id"],
            "model_kind": payload["model"]["kind"],
            "status": "queued",
            "dataset_hash": "0" * 64,
            "dataset_spec": payload["dataset"],
            "requested_at": f"2026-08-15T04:0{index}:00+00:00",
            "updated_at": f"2026-08-15T04:0{index}:00+00:00",
            "config_json": {"experiment": payload["experiment"]},
        }

    repository.create_training_job = create_job
    result = repository.create_training_experiment({
        "title": "多模型对比",
        "model_id": "ensemble-model",
        "dataset": _source(),
        "model": {"kind": "lightgbm", "params": {}},
        "search": {
            "strategy": "model_ensemble",
            "model_kinds": ["lightgbm", "xgboost", "mlp"],
            "model_params_by_kind": {
                "lightgbm": {"num_leaves": 15},
                "xgboost": {"max_depth": 4},
                "mlp": {"hidden_layers": [32, 64]},
            },
        },
    })

    assert result["strategy"] == "model_ensemble"
    assert result["shared_dataset"] is True
    assert result["dataset_count"] == 1
    assert result["trial_count"] == 3
    assert result["search_parameters"] == ["model_kind"]
    assert result["selection"]["policy"] == "alphablocks.model-ensemble-selection.v2"
    assert result["selection"]["selection_unit"] == "model_kind"
    assert [
        item["model"]["kind"] for item in captured
    ] == ["lightgbm", "xgboost", "mlp"]
    assert [
        item["experiment"]["search_params"]["model_kind"] for item in captured
    ] == ["lightgbm", "xgboost", "mlp"]
    assert captured[0]["model"]["params"]["num_leaves"] == 15
    assert captured[1]["model"]["params"]["max_depth"] == 4
    assert captured[2]["model"]["params"]["hidden_layers"] == [32, 64]
    assert all(
        item["dataset"] == _dataset_spec(_source()) for item in captured
    )


def test_model_ensemble_summary_selects_only_best_absolute_validation_icir() -> None:
    def job(index: int, kind: str, *, rank_ic: float, ic_ir: float) -> dict:
        return {
            "job_id": f"ensemble-job-{index}",
            "model_id": "ensemble-model",
            "model_version": index,
            "model_kind": kind,
            "status": "succeeded",
            "dataset_hash": "0" * 64,
            "dataset_spec": _dataset_spec(_source()),
            "requested_at": f"2026-08-15T04:0{index}:00+00:00",
            "updated_at": f"2026-08-15T05:0{index}:00+00:00",
            "config_json": {"experiment": {
                "experiment_id": "model_experiment_ensemble",
                "title": "多模型对比",
                "strategy": "model_ensemble",
                "trial_index": index,
                "trial_count": 3,
                "search_params": {"model_kind": kind},
            }},
            "result_json": {"metrics": {"validation": {
                "days": 60,
                "rank_ic": rank_ic,
                "ic_ir": ic_ir,
                "rmse": 0.7,
            }}},
        }

    result = _experiment_summary("model_experiment_ensemble", [
        job(1, "lightgbm", rank_ic=0.07, ic_ir=0.25),
        job(2, "xgboost", rank_ic=0.03, ic_ir=-0.72),
        job(3, "mlp", rank_ic=0.05, ic_ir=0.61),
    ])

    assert result["selection"]["selected_job_id"] == "ensemble-job-2"
    assert result["selection"]["selected_model_kind"] == "xgboost"
    assert result["comparison"]["selection_metric"] == "abs(validation.ic_ir)"
    assert [
        item["job_id"] for item in result["comparison"]["trials"]
        if item["is_selected"]
    ] == ["ensemble-job-2"]


def test_model_ensemble_stacking_creates_one_composite_trial() -> None:
    repository = object.__new__(ModelResearchRepository)
    captured = []

    def create_job(payload):
        captured.append(payload)
        return {
            "job_id": "stacking-job-1",
            "model_id": payload["model_id"],
            "model_kind": payload["model"]["kind"],
            "status": "queued",
            "dataset_hash": "0" * 64,
            "dataset_spec": payload["dataset"],
            "requested_at": "2026-08-15T04:01:00+00:00",
            "updated_at": "2026-08-15T04:01:00+00:00",
            "config_json": {"experiment": payload["experiment"]},
        }

    repository.create_training_job = create_job
    result = repository.create_training_experiment({
        "title": "树模型 Stacking",
        "model_id": "stacking-model",
        "dataset": _source(),
        "model": {"kind": "lightgbm", "params": {}},
        "search": {
            "strategy": "model_ensemble",
            "model_kinds": ["lightgbm", "xgboost", "linear"],
            "model_params_by_kind": {
                "lightgbm": {"num_leaves": 15},
                "xgboost": {"max_depth": 4},
                "linear": {"alpha": 3.0},
            },
            "ensemble_method": "stacking",
            "n_folds": 4,
            "meta_alpha": 2.5,
        },
    })

    assert result["trial_count"] == 1
    assert len(captured) == 1
    model = captured[0]["model"]
    assert model["kind"] == "stacking"
    assert model["params"]["n_folds"] == 4
    assert model["params"]["meta_alpha"] == 2.5
    assert model["ensemble"]["family"] == "classical"
    assert [item["kind"] for item in model["base_models"]] == [
        "lightgbm", "xgboost", "linear",
    ]
    assert captured[0]["experiment"]["search_params"]["ensemble_method"] == "stacking"


def test_model_ensemble_stacking_inherits_classification_target() -> None:
    repository = object.__new__(ModelResearchRepository)
    captured = []

    def create_job(payload):
        captured.append(payload)
        return {
            "job_id": "classification-stacking-job",
            "model_id": payload["model_id"],
            "model_kind": payload["model"]["kind"],
            "status": "queued",
            "dataset_hash": "0" * 64,
            "dataset_spec": payload["dataset"],
            "requested_at": "2026-08-15T04:01:00+00:00",
            "updated_at": "2026-08-15T04:01:00+00:00",
            "config_json": {"experiment": payload["experiment"]},
        }

    repository.create_training_job = create_job
    repository.create_training_experiment({
        "dataset": {**_source(), "target_mode": "classification"},
        "search": {
            "strategy": "model_ensemble",
            "model_kinds": ["lightgbm", "xgboost"],
            "model_params_by_kind": {"lightgbm": {}, "xgboost": {}},
            "ensemble_method": "stacking",
        },
    })

    model = captured[0]["model"]
    assert model["params"]["loss"] == "binary"
    assert model["params"]["objective"] == "binary"
    assert model["params"]["metric"] == "auc"
    assert all(
        item["params"]["loss"] == "binary" for item in model["base_models"]
    )


def test_model_ensemble_stacking_rejects_cross_family_selection() -> None:
    repository = object.__new__(ModelResearchRepository)
    repository.create_training_job = lambda _payload: None

    with pytest.raises(ModelResearchError, match="同一模型族"):
        repository.create_training_experiment({
            "dataset": _source(),
            "search": {
                "strategy": "model_ensemble",
                "model_kinds": ["lightgbm", "lstm"],
                "model_params_by_kind": {"lightgbm": {}, "lstm": {}},
                "ensemble_method": "stacking",
            },
        })


def test_model_ensemble_rejects_too_few_or_unknown_kinds() -> None:
    repository = object.__new__(ModelResearchRepository)
    repository.create_training_job = lambda _payload: None

    with pytest.raises(ModelResearchError, match="2到8"):
        repository.create_training_experiment({
            "dataset": _source(),
            "search": {
                "strategy": "model_ensemble",
                "model_kinds": ["lightgbm"],
                "model_params_by_kind": {"lightgbm": {}},
            },
        })
    with pytest.raises(ModelResearchError, match="只允许"):
        repository.create_training_experiment({
            "dataset": _source(),
            "search": {
                "strategy": "model_ensemble",
                "model_kinds": ["lightgbm", "unknown_model"],
                "model_params_by_kind": {"lightgbm": {}, "unknown_model": {}},
            },
        })


def test_model_spec_supports_all_available_model_kinds() -> None:
    expected = {
        "lightgbm": "qlib.contrib.model.gbdt.LGBModel",
        "xgboost": "qlib.contrib.model.xgboost.XGBModel",
        "catboost": "qlib.contrib.model.catboost_model.CatBoostModel",
        "random_forest": "factor_service.research.models.QlibSklearnRandomForestModel",
        "linear": "factor_service.research.models.QlibSklearnRidgeModel",
        "mlp": "factor_service.research.models.QlibTorchMLPModel",
        "gru": "factor_service.research.models.QlibTorchGRUModel",
        "lstm": "factor_service.research.models.QlibTorchLSTMModel",
        "alstm": "factor_service.research.models.QlibTorchALSTMModel",
        "transformer": "factor_service.research.models.QlibTorchTransformerModel",
        "tabnet": "factor_service.research.models.QlibNativeTabNetAdapter",
        "tcn": "factor_service.research.models.QlibTorchTCNModel",
        "nativetft": "factor_service.research.models.QlibTorchNativeTFTModel",
        "transformer_lstm": "factor_service.research.models.QlibTorchTransformerLSTMModel",
    }
    for kind, qlib_model in expected.items():
        spec = _model_spec({"kind": kind, "params": {}})
        assert spec["kind"] == kind
        assert spec["qlib_model"] == qlib_model
        assert spec["params"]["seed"] == 42
        assert spec["params"]["num_threads"] == 4
    with pytest.raises(ModelResearchError, match="只允许"):
        _model_spec({"kind": "unknown_model", "params": {}})


def test_model_spec_matches_quantmind_hyperparameter_contract() -> None:
    lightgbm = _model_spec({"kind": "lightgbm", "params": {}})["params"]
    assert lightgbm["learning_rate"] == 0.02
    assert lightgbm["n_estimators"] == 2000
    assert lightgbm["min_data_in_leaf"] == 300
    assert lightgbm["min_child_samples"] == 150
    assert lightgbm["path_smooth"] == 1.0
    assert lightgbm["bagging_freq"] == 5
    assert lightgbm["lambda_l1"] == 0.5
    assert lightgbm["lambda_l2"] == 1.0
    assert lightgbm["feature_fraction"] == 0.7
    assert lightgbm["bagging_fraction"] == 0.8

    xgboost = _model_spec({"kind": "xgboost", "params": {}})["params"]
    assert xgboost["max_depth"] == 4
    assert xgboost["subsample"] == 0.7
    assert xgboost["colsample_bytree"] == 0.65
    assert xgboost["reg_alpha"] == 0.5
    assert xgboost["reg_lambda"] == 2.0
    assert xgboost["min_child_weight"] == 100.0

    catboost = _model_spec({"kind": "catboost", "params": {}})["params"]
    assert catboost["random_strength"] == 1.5
    assert catboost["bagging_temperature"] == 0.8
    assert catboost["od_wait"] == 100

    random_forest = _model_spec({"kind": "random_forest", "params": {}})["params"]
    assert random_forest["n_estimators"] == 300
    assert random_forest["max_depth"] == 0
    assert random_forest["max_features"] == "sqrt"

    linear = _model_spec({"kind": "linear", "params": {}})["params"]
    assert linear["alpha"] == 3.0
    assert linear["fit_intercept"] is True
    mlp = _model_spec({
        "kind": "mlp", "params": {"hidden_size": 128, "layer_count": 3},
    })["params"]
    assert mlp["hidden_size"] == 128
    assert mlp["layer_count"] == 3

    gru = _model_spec({"kind": "gru", "params": {}})["params"]
    expected_gru = {
        "learning_rate": 0.001,
        "lookback_window": 20,
        "hidden_size": 64,
        "num_layers": 2,
        "dropout": 0.2,
        "max_steps": 200,
        "batch_size": 4000,
    }
    assert {key: gru[key] for key in expected_gru} == expected_gru
    assert gru["early_stopping_rounds"] == 20
    transformer = _model_spec({"kind": "transformer", "params": {}})["params"]
    assert transformer["learning_rate"] == 0.0001
    assert transformer["lookback_window"] == 20
    assert transformer["batch_size"] == 4000
    assert _model_spec({"kind": "tabnet", "params": {}})["params"][
        "learning_rate"
    ] == 0.005
    tcn = _model_spec({"kind": "tcn", "params": {}})["params"]
    assert tcn["hidden_size"] == 128
    assert tcn["num_layers"] == 2
    assert tcn["dropout"] == 0.2
    assert _model_spec({"kind": "nativetft", "params": {}})["params"][
        "learning_rate"
    ] == 0.0005


def test_next_ablation_inherits_selected_parent_and_freezes_lineage_budget() -> None:
    repository = object.__new__(ModelResearchRepository)
    captured = []
    source = {
        **_source(),
        "factors": [
            {
                "factor_id": factor_id,
                "factor_version": 1,
                "params_hash": (factor_id * 16)[:16],
                "params": {},
            }
            for factor_id in ("mom", "value", "risk")
        ],
    }
    normalized_dataset = _dataset_spec(source)
    normalized_model = _model_spec({"kind": "lightgbm", "params": {}})
    normalized_walk_forward = _walk_forward_spec({})
    parent_job = {
        "job_id": "model_job_parent",
        "model_id": "ablation-model",
        "model_kind": "lightgbm",
        "status": "succeeded",
        "dataset_spec": normalized_dataset,
        "config_json": {
            "model": normalized_model,
            "walk_forward": normalized_walk_forward,
        },
    }
    parent_summary = {
        "experiment_id": "model_experiment_parent",
        "strategy": "factor_ablation",
        "iteration": 1,
        "trial_count": 3,
        "parent_experiment_id": "",
        "selection": {
            "status": "selected",
            "selected_job_id": "model_job_parent",
        },
        "jobs": [parent_job],
    }
    repository.get_training_experiment = lambda _experiment_id: parent_summary

    def create_job(payload):
        captured.append(payload)
        index = len(captured)
        return {
            "job_id": f"next-ablation-job-{index}",
            "model_id": payload["model_id"],
            "model_kind": payload["model"]["kind"],
            "status": "queued",
            "dataset_hash": str(index) * 64,
            "dataset_spec": payload["dataset"],
            "requested_at": f"2026-08-15T03:0{index}:00+00:00",
            "updated_at": f"2026-08-15T03:0{index}:00+00:00",
            "config_json": {"experiment": payload["experiment"]},
        }

    repository.create_training_job = create_job
    result = repository.create_training_experiment({
        "title": "第二轮因子消融",
        "model_id": "ablation-model",
        "parent_experiment_id": "model_experiment_parent",
        "parent_job_id": "model_job_parent",
        "iteration": 2,
        "dataset": normalized_dataset,
        "model": normalized_model,
        "walk_forward": normalized_walk_forward,
        "search": {
            "strategy": "factor_ablation",
            "factor_ids": ["value", "risk"],
            "max_trials": 3,
        },
    })

    assert result["parent_experiment_id"] == "model_experiment_parent"
    assert result["parent_job_id"] == "model_job_parent"
    assert result["iteration"] == 2
    assert result["lineage_prior_trial_count"] == 3
    assert result["lineage_trial_count"] == 6
    assert result["lineage_trial_remaining"] == 18
    assert all(item["experiment"]["iteration"] == 2 for item in captured)

    with pytest.raises(ModelResearchConflict, match="parent_job_id"):
        repository.create_training_experiment({
            "title": "错误父任务",
            "model_id": "ablation-model",
            "parent_experiment_id": "model_experiment_parent",
            "parent_job_id": "model_job_not_selected",
            "iteration": 2,
            "dataset": normalized_dataset,
            "model": normalized_model,
            "walk_forward": normalized_walk_forward,
            "search": {
                "strategy": "factor_ablation",
                "factor_ids": ["risk"],
            },
        })

    with pytest.raises(ModelResearchConflict, match="冻结因子"):
        repository.create_training_experiment({
            "title": "篡改继承",
            "model_id": "ablation-model",
            "parent_experiment_id": "model_experiment_parent",
            "parent_job_id": "model_job_parent",
            "iteration": 2,
            "dataset": {**normalized_dataset, "date_end": "2023-12-31"},
            "model": normalized_model,
            "walk_forward": normalized_walk_forward,
            "search": {
                "strategy": "factor_ablation",
                "factor_ids": ["risk"],
            },
        })


def test_next_ablation_rejects_trials_beyond_the_lineage_budget() -> None:
    repository = object.__new__(ModelResearchRepository)
    source = {
        **_source(),
        "factors": [
            {
                "factor_id": factor_id,
                "factor_version": 1,
                "params_hash": (factor_id * 16)[:16],
                "params": {},
            }
            for factor_id in ("mom", "value")
        ],
    }
    dataset = _dataset_spec(source)
    model = _model_spec({"kind": "lightgbm", "params": {}})
    walk_forward = _walk_forward_spec({})
    parent_job = {
        "job_id": "model_job_parent",
        "model_id": "ablation-model",
        "status": "succeeded",
        "dataset_spec": dataset,
        "config_json": {"model": model, "walk_forward": walk_forward},
    }
    summaries = {
        "model_experiment_parent": {
            "strategy": "factor_ablation",
            "iteration": 2,
            "trial_count": 11,
            "parent_experiment_id": "model_experiment_root",
            "selection": {
                "status": "selected",
                "selected_job_id": "model_job_parent",
            },
            "jobs": [parent_job],
        },
        "model_experiment_root": {
            "strategy": "factor_ablation",
            "iteration": 1,
            "trial_count": 12,
            "parent_experiment_id": "",
            "selection": {"status": "selected"},
            "jobs": [],
        },
    }
    repository.get_training_experiment = lambda experiment_id: summaries[experiment_id]
    repository.create_training_job = lambda _payload: pytest.fail(
        "超预算实验不应创建任何任务"
    )

    with pytest.raises(ModelResearchConflict, match="累计最多允许24组"):
        repository.create_training_experiment({
            "title": "第三轮超预算消融",
            "model_id": "ablation-model",
            "parent_experiment_id": "model_experiment_parent",
            "parent_job_id": "model_job_parent",
            "iteration": 3,
            "dataset": dataset,
            "model": model,
            "walk_forward": walk_forward,
            "search": {
                "strategy": "factor_ablation",
                "factor_ids": ["value"],
                "max_trials": 2,
            },
        })


def test_experiment_summary_contains_history_card_metadata() -> None:
    jobs = [{
        "job_id": "job-1",
        "model_id": "grid-model",
        "model_version": 1,
        "model_kind": "lightgbm",
        "dataset_hash": "a" * 64,
        "dataset_spec": {
            "date_start": "2024-01-02", "date_end": "2024-12-31",
            "factors": [{"factor_id": "mom_20"}],
        },
        "requested_at": "2026-08-15T01:00:00+00:00",
        "updated_at": "2026-08-15T01:05:00+00:00",
        "status": "succeeded",
        "config_json": {"experiment": {
            "experiment_id": "model_experiment_test",
            "title": "参数历史",
            "trial_index": 1,
            "search_params": {"num_leaves": 31, "learning_rate": 0.05},
        }},
        "result_json": {"metrics": {
            "test_days": 80, "test_rows": 30000, "rank_ic": 0.03,
            "ic": 0.03, "ic_ir": 0.4, "rmse": 0.45,
            "validation": {
                "days": 60, "rows": 24000, "rank_ic": 0.04,
                "ic": 0.04, "ic_ir": 0.5, "rmse": 0.4,
            },
        }},
    }]

    result = _experiment_summary("model_experiment_test", jobs)

    assert result["model_kind"] == "lightgbm"
    assert result["factor_count"] == 1
    assert result["search_parameters"] == ["learning_rate", "num_leaves"]
    assert result["selection"]["status"] == "selected"
    assert result["selection"]["selected_model_version"] == 1
    comparison = result["comparison"]
    assert comparison["selection_split"] == "validation"
    assert comparison["test_metrics_role"] == "report_only"
    assert comparison["test_metrics_disclosed"] is True
    assert comparison["trials"][0]["validation"]["rank_ic"] == 0.04
    assert comparison["trials"][0]["test"]["rank_ic"] == 0.03
    assert comparison["trials"][0]["gate_passed"] is True
    assert comparison["trials"][0]["is_selected"] is True


def test_experiment_comparison_hides_test_metrics_until_all_trials_finish() -> None:
    jobs = [
        {
            "job_id": "job-1", "model_id": "grid-model", "model_version": 1,
            "status": "succeeded", "requested_at": "2026-08-15T01:00:00+00:00",
            "updated_at": "2026-08-15T01:01:00+00:00",
            "config_json": {"experiment": {"trial_index": 1}},
            "result_json": {"metrics": {
                "rank_ic": 0.08,
                "validation": {"days": 60, "rank_ic": 0.04, "ic_ir": 0.5},
            }},
        },
        {
            "job_id": "job-2", "model_id": "grid-model", "status": "running",
            "requested_at": "2026-08-15T01:02:00+00:00",
            "updated_at": "2026-08-15T01:03:00+00:00",
            "config_json": {"experiment": {"trial_index": 2}},
            "result_json": {},
        },
    ]

    result = _experiment_summary("model_experiment_running", jobs)

    assert result["selection"]["status"] == "evaluating"
    assert result["comparison"]["test_metrics_disclosed"] is False
    assert result["comparison"]["trials"][0]["validation"]["rank_ic"] == 0.04
    assert result["comparison"]["trials"][0]["test"] is None


def test_experiment_parameter_effects_are_ranked_only_by_validation_metrics() -> None:
    jobs = []
    combinations = [
        (0.03, 15, 0.02, 0.80),
        (0.03, 31, 0.04, 0.70),
        (0.05, 15, 0.06, -0.40),
        (0.05, 31, 0.08, -0.30),
    ]
    for index, (learning_rate, num_leaves, validation_ic, test_ic) in enumerate(
        combinations, start=1,
    ):
        jobs.append({
            "job_id": f"job-{index}",
            "model_id": "grid-model",
            "model_version": index,
            "model_kind": "lightgbm",
            "status": "succeeded",
            "requested_at": f"2026-08-15T01:0{index}:00+00:00",
            "updated_at": f"2026-08-15T01:1{index}:00+00:00",
            "config_json": {"experiment": {
                "trial_index": index,
                "search_params": {
                    "learning_rate": learning_rate,
                    "num_leaves": num_leaves,
                },
            }},
            "result_json": {"metrics": {
                "rank_ic": test_ic,
                "ic_ir": test_ic,
                "validation": {
                    "days": 60,
                    "rank_ic": validation_ic,
                    "ic_ir": 0.5,
                },
            }},
        })

    comparison = _experiment_summary(
        "model_experiment_parameter_effects", jobs,
    )["comparison"]

    assert comparison["summary"]["qualified_count"] == 4
    assert comparison["summary"]["qualified_ratio"] == 1.0
    effects = {
        item["parameter"]: item for item in comparison["parameter_effects"]
    }
    learning_rate = effects["learning_rate"]
    assert learning_rate["best_value"] == 0.05
    assert learning_rate["validation_rank_ic_spread"] == pytest.approx(0.04)
    best_value = next(
        item for item in learning_rate["values"] if item["is_best_validation"]
    )
    assert best_value["validation_rank_ic"]["mean"] == pytest.approx(0.07)
    # 测试集表现只做报告；即便方向与验证集相反，也不能改变最佳参数值。
    assert best_value["test_rank_ic"]["mean"] == pytest.approx(-0.35)
    assert effects["num_leaves"]["best_value"] == 31


def test_grid_search_rejects_oversized_or_unsupported_search() -> None:
    with pytest.raises(ModelResearchError, match="超过本次上限"):
        _grid_search_trials(
            {"kind": "lightgbm", "params": {}},
            {
                "parameters": {
                    "learning_rate": [0.01, 0.03, 0.05],
                    "num_leaves": [15, 31],
                },
                "max_trials": 4,
            },
        )
    with pytest.raises(ModelResearchError, match="不允许搜索参数"):
        _grid_search_trials(
            {"kind": "lightgbm", "params": {}},
            {"parameters": {"hidden_layers": [[64, 128]]}},
        )
