"""Bounded, synchronous recording of complete training metric histories."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import time
from typing import Any

from factor_service.research.job import CancellationToken, ProgressCallback


METRIC_BATCH_SIZE = 1000  # MLflow's per-request metric limit.


def log_evaluation_history(
    evaluations: Mapping[str, Mapping[str, Sequence[float]]],
    *,
    metric_prefix: str = "",
    recorder: Any | None = None,
    cancellation: CancellationToken | None = None,
    progress: ProgressCallback | None = None,
    progress_percent: int = 0,
    progress_details: Mapping[str, Any] | None = None,
    batch_size: int = METRIC_BATCH_SIZE,
) -> dict[str, Any]:
    """Preserve every (key, step, value), without Qlib's unbounded async queue.

    Use the active recorder's own client and run ID, never MLflow's mutable
    global tracking URI. Errors propagate, and completion is reported only
    after all batches have been acknowledged. SQLite durability is unchanged.
    """
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= METRIC_BATCH_SIZE
    ):
        raise ValueError(f"metric batch_size must be between 1 and {METRIC_BATCH_SIZE}")
    total = sum(
        len(values) for metrics in evaluations.values() for values in metrics.values()
    )
    batches = (total + batch_size - 1) // batch_size
    written = completed_batches = 0
    started = time.monotonic()

    def checkpoint() -> None:
        if cancellation is not None:
            cancellation.checkpoint()

    def report(stage: str) -> None:
        if progress is not None:
            progress(stage, progress_percent, {
                **(progress_details or {}),
                "metric_count": total,
                "metrics_written": written,
                "metric_batch_count": batches,
                "metric_batches_written": completed_batches,
                "metric_write_seconds": round(time.monotonic() - started, 3),
            })

    checkpoint()
    if total:
        from mlflow.entities import Metric

        if recorder is None:
            from qlib.workflow import R

            recorder = R.get_recorder()
        if not recorder.id or not callable(getattr(recorder.client, "log_batch", None)):
            raise RuntimeError("训练指标缺少有效的MLflow Recorder")
        report("training_metrics_writing")
        timestamp = int(time.time() * 1000)
        batch: list[Metric] = []

        def flush() -> None:
            nonlocal written, completed_batches
            checkpoint()
            # Explicitly synchronous even if MLFLOW_ENABLE_ASYNC_LOGGING=true.
            # At most one bounded batch is in flight; no late end_run backlog.
            recorder.client.log_batch(recorder.id, metrics=batch, synchronous=True)
            written += len(batch)
            completed_batches += 1
            batch.clear()
            checkpoint()
            report("training_metrics_writing")

        for segment, metrics in evaluations.items():
            for metric, values in metrics.items():
                key = f"{metric_prefix}{metric}.{segment}".replace("@", "_")
                for step, value in enumerate(values):
                    batch.append(Metric(key, float(value), timestamp, step))
                    if len(batch) == batch_size:
                        flush()
        if batch:
            flush()
        checkpoint()
        report("training_metrics_written")
    return {
        "metric_count": written,
        "batch_count": completed_batches,
        "duration_seconds": time.monotonic() - started,
    }
