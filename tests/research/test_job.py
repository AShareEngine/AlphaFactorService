from __future__ import annotations

import json
from copy import deepcopy
import os
from pathlib import Path
import threading

import pytest

from factor_service.research.errors import JobCanceled, PermanentJobError, WorkerShutdown
from factor_service.research.job import CancellationToken, safe_job_dir, validate_job
from factor_service.research.state import JobStateStore
from tests.research.utils import valid_inference_job, valid_job


def test_job_validation_accepts_frozen_contract() -> None:
    job = validate_job(valid_job())

    assert job["model_id"] == "test_model"
    assert job["dataset_spec"]["universe_id"] == "csi500"


def test_job_validation_accepts_all_phase2_model_kinds() -> None:
    source = valid_job()
    cases = {
        "xgboost": {"max_depth": 4, "n_estimators": 20},
        "catboost": {"depth": 4, "n_estimators": 20},
        "mlp": {"hidden_layers": [16, 32, 64], "max_steps": 20, "batch_size": 32},
        "lstm": {
            "lookback_window": 20, "hidden_size": 32, "num_layers": 2,
            "dropout": 0.1, "max_steps": 20, "batch_size": 32,
        },
        "transformer_lstm": {
            "lookback_window": 20, "d_model": 32, "nhead": 4,
            "transformer_layers": 1, "dim_feedforward": 64,
            "lstm_hidden_size": 32, "lstm_layers": 1,
            "dropout": 0.1, "max_steps": 20, "batch_size": 32,
        },
    }
    for kind, params in cases.items():
        candidate = deepcopy(source)
        candidate["config_json"]["model"] = {"kind": kind, "params": params}
        assert validate_job(candidate)["config_json"]["model"]["kind"] == kind


def test_job_validation_accepts_walk_forward_contract() -> None:
    source = valid_job()
    source["config_json"]["walk_forward"] = {
        "enabled": True,
        "strategy": "rolling",
        "train_years": 1,
        "valid_months": 3,
        "test_months": 12,
        "step_months": 12,
        "max_windows": 4,
        "embargo_days": 5,
    }

    job = validate_job(source)

    assert job["config_json"]["walk_forward"]["train_years"] == 1


def test_job_validation_accepts_strict_lightgbm_incremental_contract() -> None:
    source = valid_job()
    source["config_json"]["planned_model_version"] = 2
    source["config_json"]["incremental_training"] = {
        "schema_version": "alphablocks.incremental-training.v1",
        "mode": "lightgbm_append_trees_new_data_only",
        "source_model_id": "test_model",
        "source_model_version": 1,
        "source_job_id": "model_job_source",
        "source_dataset_hash": "c" * 64,
        "source_date_end": "2024-06-28",
        "candidate_date_end": "2024-12-31",
        "minimum_new_trading_sessions": 60,
        "source_artifact": {
            "artifact_id": "artifact_model_bundle",
            "relative_path": "model_job_source/bundle/source.tar.gz",
            "sha256": "b" * 64,
            "file_name": "source.tar.gz",
        },
        "allowed_parameter_changes": ["n_estimators", "early_stopping_rounds"],
    }

    job = validate_job(source)

    assert job["config_json"]["incremental_training"]["source_model_version"] == 1


def test_job_validation_rejects_incremental_path_traversal_or_deep_model() -> None:
    source = valid_job()
    source["config_json"]["planned_model_version"] = 2
    contract = {
        "schema_version": "alphablocks.incremental-training.v1",
        "mode": "lightgbm_append_trees_new_data_only",
        "source_model_id": "test_model",
        "source_model_version": 1,
        "source_job_id": "model_job_source",
        "source_date_end": "2024-06-28",
        "minimum_new_trading_sessions": 60,
        "source_artifact": {
            "artifact_id": "artifact_model_bundle",
            "relative_path": "../source.tar.gz",
            "sha256": "b" * 64,
        },
    }
    source["config_json"]["incremental_training"] = contract
    with pytest.raises(PermanentJobError, match="路径无效"):
        validate_job(source)

    source = valid_job()
    source["config_json"]["planned_model_version"] = 2
    source["config_json"]["model"] = {
        "kind": "lstm", "params": {"max_steps": 20, "batch_size": 32},
    }
    source["config_json"]["incremental_training"] = {
        **contract,
        "source_artifact": {
            **contract["source_artifact"],
            "relative_path": "models/source.tar.gz",
        },
    }
    with pytest.raises(PermanentJobError, match="只支持LightGBM"):
        validate_job(source)


def test_job_validation_rejects_overlapping_walk_forward_tests() -> None:
    source = valid_job()
    source["config_json"]["walk_forward"] = {
        "enabled": True,
        "test_months": 12,
        "step_months": 6,
    }

    with pytest.raises(PermanentJobError, match="步长不得小于测试窗口"):
        validate_job(source)


def test_job_validation_rejects_tampered_dataset() -> None:
    job = valid_job()
    job["dataset_spec"]["date_end"] = "2026-01-01"

    with pytest.raises(PermanentJobError, match="dataset_hash"):
        validate_job(job)


def test_job_validation_rejects_divergent_config_dataset() -> None:
    job = valid_job()
    job["config_json"]["dataset"]["date_end"] = "2026-01-01"

    with pytest.raises(PermanentJobError, match="config_json.dataset"):
        validate_job(job)


def test_job_validation_rejects_unbounded_model_parameter() -> None:
    job = valid_job()
    job["config_json"]["model"]["params"]["num_threads"] = 1000

    with pytest.raises(PermanentJobError, match="num_threads"):
        validate_job(job)


@pytest.mark.parametrize("layers", [[], [2, 64], [64] * 9, "64,128"])
def test_job_validation_rejects_invalid_mlp_hidden_layers(layers) -> None:
    job = valid_job()
    job["config_json"]["model"] = {
        "kind": "mlp",
        "params": {"hidden_layers": layers, "max_steps": 20, "batch_size": 32},
    }

    with pytest.raises(PermanentJobError, match="hidden_layers"):
        validate_job(job)


@pytest.mark.parametrize(
    ("field", "value"), [("lookback_window", 1), ("num_layers", 9), ("dropout", 1.0)],
)
def test_job_validation_rejects_invalid_lstm_architecture(field, value) -> None:
    job = valid_job()
    job["config_json"]["model"] = {
        "kind": "lstm",
        "params": {field: value, "max_steps": 20, "batch_size": 32},
    }

    with pytest.raises(PermanentJobError, match=field):
        validate_job(job)


def test_job_validation_rejects_incompatible_transformer_attention_heads() -> None:
    job = valid_job()
    job["config_json"]["model"] = {
        "kind": "transformer_lstm",
        "params": {"d_model": 30, "nhead": 8, "max_steps": 20, "batch_size": 32},
    }

    with pytest.raises(PermanentJobError, match="d_model.*nhead"):
        validate_job(job)


def test_job_validation_rejects_non_object_config() -> None:
    job = valid_job()
    job["config_json"] = []

    with pytest.raises(PermanentJobError, match="config_json"):
        validate_job(job)


def test_job_validation_accepts_daily_inference_contract() -> None:
    job = validate_job(valid_inference_job())

    assert job["kind"] == "infer"
    assert job["config_json"]["inference"]["trade_date"] == "2024-12-31"


def test_job_validation_rejects_inference_cutoff_before_signal_close() -> None:
    job = valid_inference_job()
    job["config_json"]["inference"]["data_cutoff"] = "2024-12-31T06:00:00+00:00"

    with pytest.raises(PermanentJobError, match="data_cutoff"):
        validate_job(job)


def test_safe_job_dir_rejects_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(PermanentJobError):
        safe_job_dir(tmp_path, "../outside")


def test_cancellation_and_shutdown_are_distinct() -> None:
    token = CancellationToken()
    token.cancel("manual cancel")
    with pytest.raises(JobCanceled, match="manual cancel"):
        token.checkpoint()

    shutdown = threading.Event()
    shutdown.set()
    with pytest.raises(WorkerShutdown):
        CancellationToken(shutdown).checkpoint()


def test_job_state_is_atomic_private_and_round_trips(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path)
    job = valid_job()
    store.save(job, "training", {"percent": 60})

    state = store.load()
    assert state is not None
    assert state["job"]["lease_token"] == job["lease_token"]
    assert state["phase"] == "training"
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    json.loads(store.path.read_text(encoding="utf-8"))
    store.clear()
    assert store.load() is None
