from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from factor_service.research.errors import JobCanceled
from factor_service.research.job import CancellationToken
from factor_service.research.metric_logging import log_evaluation_history


class RecordingClient:
    def __init__(self):
        self.batches = []

    def log_batch(self, run_id, *, metrics, synchronous):
        assert synchronous is True
        self.batches.append((run_id, list(metrics)))


def test_batches_preserve_all_metric_keys_steps_and_values(monkeypatch):
    monkeypatch.setenv("MLFLOW_ENABLE_ASYNC_LOGGING", "true")
    values = np.arange(1003, dtype=float) / 2000
    evaluations = {"train": {"rmse": values}, "valid": {"ndcg@5": values + 0.1}}
    client = RecordingClient()
    recorder = SimpleNamespace(id="the-active-run", client=client)
    progress = []

    result = log_evaluation_history(
        evaluations, recorder=recorder, metric_prefix="optuna.trial_2.fold_1.",
        progress=lambda *args: progress.append(args), progress_percent=63,
        progress_details={"trial": 2, "tuning_fold": 1, "training_stage": "optuna_trial"},
    )

    assert [len(batch) for _, batch in client.batches] == [1000, 1000, 6]
    assert {run_id for run_id, _ in client.batches} == {"the-active-run"}
    actual = [(m.key, m.step, m.value) for _, batch in client.batches for m in batch]
    assert actual == [
        (f"optuna.trial_2.fold_1.{metric}.{segment}".replace("@", "_"), step, float(value))
        for segment, metrics in evaluations.items()
        for metric, series in metrics.items()
        for step, value in enumerate(series)
    ]
    assert result["metric_count"] == 2006
    assert result["batch_count"] == 3
    assert [item[2]["metrics_written"] for item in progress] == [0, 1000, 2000, 2006, 2006]
    assert progress[-1][0] == "training_metrics_written"
    assert all(item[1] == 63 and item[2]["trial"] == 2 for item in progress)
    assert progress[-1][2]["training_stage"] == "optuna_trial"


def test_empty_metrics_do_not_require_a_recorder():
    result = log_evaluation_history({"train": {"rmse": []}})
    assert result["metric_count"] == result["batch_count"] == 0


@pytest.mark.parametrize("batch_size", [0, -1, 1001, True, 1.5, "1000"])
def test_metric_batch_size_is_bounded(batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        log_evaluation_history({}, batch_size=batch_size)


def test_logging_failure_is_not_swallowed_or_reported_as_complete():
    client = RecordingClient()
    original = client.log_batch

    def fail_second_batch(*args, **kwargs):
        if client.batches:
            raise OSError("disk full")
        original(*args, **kwargs)

    client.log_batch = fail_second_batch
    progress = []
    with pytest.raises(OSError, match="disk full"):
        log_evaluation_history(
            {"train": {"rmse": [0.5] * 1001}},
            recorder=SimpleNamespace(id="run", client=client),
            progress=lambda *args: progress.append(args),
        )
    assert len(client.batches) == 1
    assert progress[-1][2]["metrics_written"] == 1000
    assert all(item[0] != "training_metrics_written" for item in progress)


def test_cancellation_stops_between_batches():
    token = CancellationToken()
    client = RecordingClient()
    progress = []

    def report(*args):
        progress.append(args)
        if args[2]["metrics_written"] == 1000:
            token.cancel("test canceled")

    with pytest.raises(JobCanceled, match="test canceled"):
        log_evaluation_history(
            {"train": {"rmse": [0.5] * 2001}},
            recorder=SimpleNamespace(id="run", client=client),
            cancellation=token, progress=report,
        )
    assert len(client.batches) == 1
    assert all(item[0] != "training_metrics_written" for item in progress)


def test_canceled_before_logging_performs_no_writes():
    token = CancellationToken()
    token.cancel()
    client = RecordingClient()
    with pytest.raises(JobCanceled):
        log_evaluation_history(
            {"train": {"rmse": [0.5]}},
            recorder=SimpleNamespace(id="run", client=client), cancellation=token,
        )
    assert not client.batches


def test_cancellation_after_final_batch_is_not_reported_complete():
    token = CancellationToken()
    progress = []

    def report(*args):
        progress.append(args)
        if args[2]["metrics_written"]:
            token.cancel()

    with pytest.raises(JobCanceled):
        log_evaluation_history(
            {"train": {"rmse": [0.5]}},
            recorder=SimpleNamespace(id="run", client=RecordingClient()),
            cancellation=token, progress=report,
        )
    assert all(item[0] != "training_metrics_written" for item in progress)


def test_real_mlflow_sqlite_history_is_complete_and_run_scoped(tmp_path, monkeypatch):
    import mlflow
    from mlflow.tracking import MlflowClient
    from qlib.workflow.recorder import MLflowRecorder

    uri = f"sqlite:///{tmp_path / 'metrics.db'}"
    client = MlflowClient(tracking_uri=uri)
    experiment = client.create_experiment("batch-metrics", artifact_location=(tmp_path / "artifacts").as_uri())
    first = client.create_run(experiment)
    second = client.create_run(experiment)
    recorder = MLflowRecorder(experiment, uri, mlflow_run=first)
    recorder.log_metrics = Mock(side_effect=AssertionError("must not enqueue individual writes"))
    monkeypatch.setenv("MLFLOW_ENABLE_ASYNC_LOGGING", "true")
    previous_uri = mlflow.get_tracking_uri()
    try:
        # A different global destination must not steal another recorder's metrics.
        mlflow.set_tracking_uri("http://127.0.0.1:1/unreachable")
        for prefix, count in [("optuna.trial_1.fold_1.", 1101), ("optuna.trial_2.fold_2.", 17)]:
            evaluations = {"train": {"rmse": np.arange(count) / 2000}, "valid": {"ndcg@5": [0.6] * count}}
            result = log_evaluation_history(evaluations, recorder=recorder, metric_prefix=prefix)
            assert result["metric_count"] == 2 * count
            for segment, metrics in evaluations.items():
                for key, values in metrics.items():
                    metric_key = f"{prefix}{key}.{segment}".replace("@", "_")
                    history = client.get_metric_history(first.info.run_id, metric_key)
                    assert sorted((m.step, m.value) for m in history) == list(enumerate(values))
                    assert not client.get_metric_history(second.info.run_id, metric_key)
        assert recorder.async_log is None
        recorder.log_metrics.assert_not_called()
    finally:
        mlflow.set_tracking_uri(previous_uri)
        client.set_terminated(first.info.run_id)
        client.set_terminated(second.info.run_id)


def test_metric_progress_milestones_are_persisted():
    from factor_service.research.worker import PERSISTED_PROGRESS_STAGES

    for stage in ("training_metrics_writing", "training_metrics_written"):
        assert stage in PERSISTED_PROGRESS_STAGES
        assert f"remote.{stage}" in PERSISTED_PROGRESS_STAGES
