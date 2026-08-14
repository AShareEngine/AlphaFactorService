from __future__ import annotations

from hashlib import sha256
import json
from copy import deepcopy
from typing import Any


def valid_job(*, job_id: str = "model_job_test", **changes: Any) -> dict[str, Any]:
    spec = {
        "name": "test dataset",
        "universe_id": "csi500",
        "index_code": "000905.SH",
        "date_start": "2020-01-02",
        "date_end": "2024-12-31",
        "data_cutoff": "2025-01-01T00:00:00+00:00",
        "factors": [{
            "factor_id": "mean_amount",
            "factor_version": 1,
            "params_hash": "a" * 64,
            "label": "mean amount",
            "category": "volume",
        }],
        "feature_field": "score",
        "label": {"kind": "future_5d_cross_sectional_rank"},
        "split": {"train": 0.6, "valid": 0.2, "test": 0.2, "embargo_days": 5},
        "minimum_factor_coverage": 0.8,
        "availability": {
            "event_available_at_lte_signal_close": True,
            "computed_at_lte_data_cutoff": True,
        },
    }
    dataset_hash = sha256(json.dumps(
        spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    result = {
        "job_id": job_id,
        "model_id": "test_model",
        "lease_token": "lease-token-at-least-sixteen-characters",
        "lease_owner": "alpha-research-worker",
        "dataset_hash": dataset_hash,
        "dataset_spec": spec,
        "config_json": {
            "planned_model_version": 1,
            "dataset": deepcopy(spec),
            "model": {"kind": "lightgbm", "params": {"n_estimators": 100}},
        },
    }
    result.update(changes)
    return result


def valid_inference_job(*, job_id: str = "model_job_infer", **changes: Any) -> dict[str, Any]:
    job = valid_job(job_id=job_id)
    job["kind"] = "infer"
    job["model_version"] = 1
    job["config_json"] = {
        "schema_version": "alphablocks.model-inference.v1",
        "planned_model_version": 1,
        "dataset": deepcopy(job["dataset_spec"]),
        "source_model": {
            "model_id": "test_model",
            "model_version": 1,
            "training_job_id": "model_job_training",
            "artifact_id": "artifact_model_bundle",
            "artifact_sha256": "b" * 64,
            "artifact_file_name": "qlib_experiment.tar.gz",
        },
        "inference": {
            "trade_date": "2024-12-31",
            "data_cutoff": "2024-12-31T08:00:00+00:00",
            "feature_cutoff_at": "2024-12-31T07:00:00+00:00",
        },
    }
    job.update(changes)
    return job
