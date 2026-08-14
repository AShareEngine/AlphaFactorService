from pathlib import Path
from types import SimpleNamespace

from factor_service.model_artifacts import ModelArtifactStore
from factor_service.research.control import ResearchControl
from factor_service.research.worker import ResearchWorker


class _Repository:
    def __init__(self) -> None:
        self.artifacts: list[dict] = []

    def worker_control(self, job_id, *, lease_token):
        return {"job_id": job_id, "status": "running", "lease_token": lease_token}

    def record_artifact(self, **payload):
        self.artifacts.append(payload)
        return {"artifact_id": "artifact-1", **payload}


def test_artifact_registration_writes_metadata_directly(tmp_path: Path) -> None:
    repository = _Repository()
    control = ResearchControl(
        repository,  # type: ignore[arg-type]
        ModelArtifactStore(tmp_path / "artifacts"),
    )

    response = control.record_artifact(
        "job-1",
        "lease-1",
        kind="bundle",
        file_name="model.bin",
        relative_path="job-1/bundle/model.bin",
        digest="a" * 64,
        size_bytes=8,
        dataset_hash="b" * 64,
    )

    assert response["artifact"]["artifact_id"] == "artifact-1"
    assert repository.artifacts == [{
        "job_id": "job-1",
        "artifact_kind": "bundle",
        "file_name": "model.bin",
        "relative_path": "job-1/bundle/model.bin",
        "digest": "a" * 64,
        "size_bytes": 8,
        "dataset_hash": "b" * 64,
    }]


def test_scheduler_push_is_idempotent_for_same_active_job() -> None:
    worker = ResearchWorker.__new__(ResearchWorker)
    worker.active_job_id = "job-1"
    worker.last_job_id = "job-1"
    worker.last_job_status = "running"
    worker.last_error = ""
    worker.stopping = False
    worker.recovery_pending = False
    worker._state_lock = __import__("threading").Lock()
    worker._job_thread = SimpleNamespace(is_alive=lambda: True)

    result = worker.submit({"job_id": "job-1", "lease_token": "lease-1"})

    assert result["accepted"] is True
    assert result["duplicate"] is True
    assert "node_id" not in result
