from __future__ import annotations

from pathlib import Path

import pytest

from factor_service.research.artifact_recovery import (
    _validate_recovery_identity,
)
from factor_service.research.trainer import TrainingResult


def _job() -> dict:
    return {
        "job_id": "job-recovery",
        "kind": "train",
        "status": "canceled",
        "model_id": "model-recovery",
        "dataset_hash": "a" * 64,
        "config_json": {"planned_model_version": 3},
        "attempts": [{"ordinal": 2, "status": "canceled"}],
    }


def _trained(tmp_path: Path) -> tuple[TrainingResult, Path]:
    result_path = tmp_path / "remote_result.json"
    result_path.write_text("{}", encoding="utf-8")
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(b"bundle")
    predictions = tmp_path / "predictions.parquet"
    predictions.write_bytes(b"predictions")
    return TrainingResult(
        result={
            "manifest": {
                "model_id": "model-recovery",
                "dataset_hash": "a" * 64,
            },
            "predictions": {"model_version": 3, "row_count": 10},
        },
        artifacts=[("bundle", bundle)],
        predictions_path=predictions,
    ), result_path


def test_recovery_identity_preserves_job_model_dataset_and_version(
    tmp_path: Path,
) -> None:
    trained, result_path = _trained(tmp_path)

    evidence = _validate_recovery_identity(
        _job(),
        trained,
        source_attempt=2,
        result_path=result_path,
    )

    assert evidence["source_attempt"] == 2
    assert evidence["model_id"] == "model-recovery"
    assert evidence["planned_model_version"] == 3
    assert evidence["dataset_hash"] == "a" * 64
    assert evidence["prediction_rows"] == 10
    assert evidence["artifacts"][0]["sha256"]


def test_recovery_identity_rejects_version_mismatch(tmp_path: Path) -> None:
    trained, result_path = _trained(tmp_path)
    trained.result["predictions"]["model_version"] = 4

    with pytest.raises(ValueError, match="预留版本不一致"):
        _validate_recovery_identity(
            _job(),
            trained,
            source_attempt=2,
            result_path=result_path,
        )
