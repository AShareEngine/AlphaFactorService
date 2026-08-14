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
        "mlp": {"hidden_size": 16, "layer_count": 2, "max_steps": 20, "batch_size": 32},
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
