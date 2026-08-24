from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from factor_service.research.errors import JobCanceled, WorkerShutdown
from factor_service.research.job import CancellationToken
from factor_service.research.trainer import TrainingResult
from factor_service.research.worker import ResearchWorker
from tests.research.utils import valid_job


def _settings(tmp_path: Path):
    return SimpleNamespace(
        work_root=tmp_path,
        model_artifacts_root=tmp_path / "artifacts",
    )


class _Api:
    def __init__(self) -> None:
        self.failed: list[tuple[str, bool]] = []
        self.failure_messages: list[str] = []
        self.completed: list[str] = []
        self.artifacts: list[dict] = []

    def renew(self, *_args, **_kwargs):
        return {"ok": True}

    def control(self, *_args, **_kwargs):
        return {"status": "running", "cancel_requested": False}

    def stage(self, *_args, **_kwargs):
        return {"ok": True}

    def record_artifact(self, *_args, **kwargs):
        self.artifacts.append(dict(kwargs))
        return {"ok": True}

    def complete(self, job_id, _lease_token, _result):
        self.completed.append(job_id)
        return {"ok": True}

    def fail(self, job_id, _lease_token, _error, retryable=True):
        self.failed.append((job_id, retryable))
        self.failure_messages.append(_error)
        return {"ok": True, "job": {"status": "queued" if retryable else "failed"}}


class _FailingTrainer:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def train(self, *_args, **_kwargs):
        raise self.error


class _SuccessfulTrainer:
    def __init__(self, artifact: Path, predictions: Path) -> None:
        self.artifact = artifact
        self.predictions = predictions

    def train(self, *_args, **_kwargs):
        return TrainingResult(
            result={"predictions": {"row_count": 0}},
            artifacts=[("bundle", self.artifact)],
            predictions_path=self.predictions,
        )

    def publish_predictions(self, *_args, **_kwargs):
        return 0


def test_successful_job_publishes_artifact_locally_then_records_metadata(tmp_path: Path) -> None:
    artifact = tmp_path / "qlib_experiment.tar.gz"
    predictions = tmp_path / "predictions.parquet"
    artifact.write_bytes(b"formal model bundle")
    predictions.write_bytes(b"")
    worker = ResearchWorker(_settings(tmp_path / "work"))
    api = _Api()
    worker.control = api
    worker.trainer = _SuccessfulTrainer(artifact, predictions)

    worker._run_job(valid_job())

    assert api.completed == ["model_job_test"]
    assert len(api.artifacts) == 1
    registered = api.artifacts[0]
    assert registered["kind"] == "bundle"
    assert registered["file_name"] == "qlib_experiment.tar.gz"
    assert registered["relative_path"].endswith("bundle/qlib_experiment.tar.gz")
    assert worker.artifact_store.resolve(registered["relative_path"]).read_bytes() == b"formal model bundle"


def test_retry_attempt_creates_fresh_work_directory(tmp_path: Path) -> None:
    artifact = tmp_path / "qlib_experiment.tar.gz"
    predictions = tmp_path / "predictions.parquet"
    artifact.write_bytes(b"formal model bundle")
    predictions.write_bytes(b"")

    class _WorkDirCheckingTrainer(_SuccessfulTrainer):
        def train(self, _job, work_dir, **_kwargs):
            assert work_dir.name == "attempt-002"
            assert work_dir.is_dir()
            return super().train()

    worker = ResearchWorker(_settings(tmp_path / "work"))
    api = _Api()
    worker.control = api
    worker.trainer = _WorkDirCheckingTrainer(artifact, predictions)
    job = valid_job()
    job["attempt_count"] = 2

    worker._run_job(job)

    assert api.completed == ["model_job_test"]


def test_invalid_data_failure_is_not_retried(tmp_path: Path) -> None:
    worker = ResearchWorker(_settings(tmp_path))
    api = _Api()
    worker.control = api
    worker.trainer = _FailingTrainer(ValueError("bad frozen data"))

    worker._run_job(valid_job())

    assert api.failed == [("model_job_test", False)]
    assert worker.state_store.load() is None


def test_shutdown_failure_is_requeued(tmp_path: Path) -> None:
    worker = ResearchWorker(_settings(tmp_path))
    api = _Api()
    worker.control = api
    worker.trainer = _FailingTrainer(WorkerShutdown("restart"))

    worker._run_job(valid_job())

    assert api.failed == [("model_job_test", True)]


def test_lease_monitor_observes_remote_cancel(tmp_path: Path) -> None:
    worker = ResearchWorker(_settings(tmp_path))

    class _CancelApi(_Api):
        def control(self, *_args, **_kwargs):
            return {"status": "running", "cancel_requested": True}

    class _OneTick:
        calls = 0

        def wait(self, _seconds):
            self.calls += 1
            return self.calls > 1

    worker.control = _CancelApi()
    cancellation = CancellationToken()
    worker._monitor_lease("model_job_test", "lease", cancellation, _OneTick())

    with pytest.raises(JobCanceled, match="用户"):
        cancellation.checkpoint()


def test_restart_recovery_requeues_active_job(tmp_path: Path) -> None:
    worker = ResearchWorker(_settings(tmp_path))
    api = _Api()
    worker.control = api
    job = valid_job()
    worker.state_store.save(job, "training", {"percent": 60})
    worker.recovery_pending = True

    worker._recover_interrupted_job()

    assert api.failed == [("model_job_test", True)]
    assert worker.state_store.load() is None
    assert worker.recovery_pending is False
    assert worker.last_job_status == "queued"


def test_recovery_reports_original_pending_failure(tmp_path: Path) -> None:
    worker = ResearchWorker(_settings(tmp_path))
    api = _Api()
    worker.control = api
    job = valid_job()
    worker.state_store.save(job, "failure_report_pending", {
        "error_message": "[control_database_transient] PostgreSQL unavailable",
        "retryable": True,
    })
    worker.recovery_pending = True

    worker._recover_interrupted_job()

    assert api.failed == [("model_job_test", True)]
    assert api.failure_messages == ["[control_database_transient] PostgreSQL unavailable"]


def test_progress_state_survives_process_boundary(tmp_path: Path) -> None:
    worker = ResearchWorker(_settings(tmp_path))
    worker.control = _Api()
    job = valid_job()
    cancellation = CancellationToken()

    worker._report_progress(job, cancellation, "loading_factors", 22, {"factor_index": 1})

    saved = worker.state_store.load()
    assert saved is not None
    assert saved["phase"] == "loading_factors"
    assert saved["progress"]["percent"] == 22


def test_acceptance_state_write_failure_does_not_leave_worker_busy(tmp_path: Path) -> None:
    worker = ResearchWorker(_settings(tmp_path))

    class _BrokenState:
        @staticmethod
        def save(*_args, **_kwargs):
            raise OSError("disk full")

    worker.state_store = _BrokenState()

    with pytest.raises(OSError, match="disk full"):
        worker.submit(valid_job())

    assert worker.active_job_id == ""
    assert worker.active_lease_token == ""
    assert worker.recovery_pending is False
