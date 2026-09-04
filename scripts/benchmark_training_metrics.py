"""Compare complete Qlib per-point logging with bounded MLflow batches.

Uses synthetic metric values and an isolated, new SQLite database. Does not
query market data, create training jobs, or touch a running job's recorder.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=500)
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.steps <= 2000 or not 1 <= args.repeats <= 5:
        parser.error("steps must be 1..2000, repeats must be 1..5")
    args.work_dir.mkdir(parents=True, exist_ok=False)
    # Match the production recorder's synchronous underlying metric writes;
    # the old path below still includes Qlib's async queue and its final drain.
    os.environ["MLFLOW_ENABLE_ASYNC_LOGGING"] = "false"

    from mlflow.tracking import MlflowClient
    from qlib.utils.paral import AsyncCaller
    from qlib.workflow.recorder import MLflowRecorder
    from factor_service.research.metric_logging import log_evaluation_history
    from factor_service.research.runtime_resources import read_runtime_resources

    uri = f"sqlite:///{(args.work_dir / 'benchmark.db').resolve().as_posix()}"
    client = MlflowClient(tracking_uri=uri)
    experiment_id = client.create_experiment(
        "synthetic-metric-logging", artifact_location=(args.work_dir / "artifacts").resolve().as_uri(),
    )
    evaluations = {
        "train": {"rmse": [0.6 - i / (10 * args.steps) for i in range(args.steps)]},
        "valid": {"rmse": [0.65 - i / (20 * args.steps) for i in range(args.steps)]},
    }
    samples = []
    for repeat in range(args.repeats):
        # Reverse order on alternate repeats to reduce warm-cache bias.
        modes = ("per_point", "batch") if repeat % 2 == 0 else ("batch", "per_point")
        for mode in modes:
            run = client.create_run(experiment_id)
            recorder = MLflowRecorder(experiment_id, uri, mlflow_run=run)
            if mode == "per_point":
                recorder.async_log = AsyncCaller()
            started = time.perf_counter()
            if mode == "per_point":
                for segment, metrics in evaluations.items():
                    for metric, values in metrics.items():
                        for step, value in enumerate(values):
                            recorder.log_metrics(**{f"{metric}.{segment}": value}, step=step)
                recorder.async_log.wait()
                recorder.async_log = None
                calls = 2 * args.steps
            else:
                result = log_evaluation_history(evaluations, recorder=recorder)
                calls = result["batch_count"]
            elapsed = time.perf_counter() - started
            # Validate every stored point, not just MLflow's last metric value.
            for segment, metrics in evaluations.items():
                for metric, values in metrics.items():
                    history = client.get_metric_history(run.info.run_id, f"{metric}.{segment}")
                    actual = sorted((item.step, item.value) for item in history)
                    if actual != list(enumerate(values)):
                        raise AssertionError(f"Incomplete metric history: {mode}/{segment}/{metric}")
            client.set_terminated(run.info.run_id)
            sample = {
                "mode": mode, "repeat": repeat + 1, "seconds": elapsed,
                "metric_count": 2 * args.steps, "write_calls": calls,
                "all_points_verified": True,
            }
            samples.append(sample)
            # Preserve each verified sample even if the SSH client disconnects.
            (args.work_dir / "samples.json").write_text(json.dumps(samples, indent=2), encoding="utf-8")
            print(json.dumps(sample), flush=True)
    medians = {
        mode: statistics.median(row["seconds"] for row in samples if row["mode"] == mode)
        for mode in ("per_point", "batch")
    }
    report = {
        "schema_version": "alphablocks.metric-logging-benchmark.v1",
        "synthetic": True, "steps_per_series": args.steps, "series": 2,
        "samples": samples, "median_seconds": medians,
        "speedup": medians["per_point"] / medians["batch"],
        "resources": read_runtime_resources().public(),
        "note": "Logging-only microbenchmark; not an end-to-end training speedup. SQLite durability unchanged.",
    }
    (args.work_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
