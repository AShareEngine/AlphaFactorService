from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from factor_service.model_artifacts import ModelArtifactStore
from factor_service.research.control import ResearchControl
from factor_service.research.worker import ResearchWorker


class _Repository:
    def __init__(self) -> None:
        self.artifacts: list[dict] = []
        self.finalized_jobs: list[str] = []

    def worker_control(self, job_id, *, lease_token):
        return {"job_id": job_id, "status": "running", "lease_token": lease_token}

    def record_artifact(self, **payload):
        self.artifacts.append(payload)
        return {"artifact_id": "artifact-1", **payload}

    def get_artifact(self, artifact_id):
        return next(item for item in self.artifacts if item["artifact_id"] == artifact_id)

    def complete_job(self, job_id, *, lease_token, result):
        return {
            "job_id": job_id,
            "kind": "train",
            "status": "succeeded",
            "result_json": result,
        }

    def finalize_training_result(self, job_id):
        self.finalized_jobs.append(job_id)
        return {
            "job_id": job_id,
            "kind": "train",
            "status": "succeeded",
            "model_version": 3,
            "registration_status": "registered",
        }


class _ObjectStore:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.downloads = 0

    def download_file(self, **payload):
        self.downloads += 1
        destination = Path(payload["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.body)
        return destination


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
        "object_store_uri": "",
        "object_store_version_id": "",
        "object_store_sha256": "",
    }]


def test_artifact_download_falls_back_to_minio_and_reuses_local_cache(tmp_path: Path) -> None:
    body = b"portable candidate model"
    digest = sha256(body).hexdigest()
    repository = _Repository()
    repository.artifacts.append({
        "artifact_id": "artifact-remote",
        "artifact_kind": "bundle",
        "file_name": "model.tar.gz",
        "relative_path": "another-machine/bundle/model.tar.gz",
        "sha256": digest,
        "size_bytes": len(body),
        "object_store_uri": "s3://alphablocks-models/models/model.tar.gz",
        "object_store_version_id": "version-7",
        "object_store_sha256": digest,
    })
    object_store = _ObjectStore(body)
    control = ResearchControl(
        repository,  # type: ignore[arg-type]
        ModelArtifactStore(tmp_path / "artifacts"),
        object_store,  # type: ignore[arg-type]
    )

    first = control.download_artifact(
        "artifact-remote", tmp_path / "run-1" / "model.tar.gz", digest,
    )
    second = control.download_artifact(
        "artifact-remote", tmp_path / "run-2" / "model.tar.gz", digest,
    )

    assert first.read_bytes() == body
    assert second.read_bytes() == body
    assert object_store.downloads == 1


def test_training_completion_automatically_finalizes_candidate_registration(
    tmp_path: Path,
) -> None:
    repository = _Repository()
    control = ResearchControl(
        repository,  # type: ignore[arg-type]
        ModelArtifactStore(tmp_path / "artifacts"),
    )

    response = control.complete("job-1", "lease-1", {"metrics": {"rank_ic": 0.03}})

    assert response["job"]["model_version"] == 3
    assert response["job"]["registration_status"] == "registered"
    assert repository.finalized_jobs == ["job-1"]


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
