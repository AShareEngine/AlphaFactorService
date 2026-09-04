"""Bounded synthetic regression check; never registers models or queries data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

from factor_service.research.runtime_resources import read_runtime_resources


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--windows", type=int, default=8, choices=range(2, 38))
    args = parser.parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=False)
    root = args.work_dir.resolve()
    resources = read_runtime_resources()
    threads = min(4, resources.cpu_cores)
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "ALPHA_EFFECTIVE_NUM_THREADS"):
        os.environ[key] = str(threads)

    import numpy as np
    import pandas as pd
    import qlib
    from qlib.data.dataset import DataHandlerLP, DatasetH
    from factor_service.research.dataset import PreparedDataset
    from factor_service.research.trainer import _prepare_recorder_experiment, _run_walk_forward

    dates = pd.bdate_range("2020-01-02", periods=283 + args.windows * 5)
    # Deliberately synthetic, not real prices, factors or financial evaluation.
    index = pd.MultiIndex.from_product(
        [dates, [f"SYNTH-{i}" for i in range(256)]], names=["datetime", "instrument"],
    )
    rng = np.random.default_rng(42)
    values = rng.normal(size=(len(index), 17))
    columns = pd.MultiIndex.from_tuples([
        *[("feature", f"synthetic_{i}") for i in range(16)], ("label", "LABEL0"),
    ])
    frame = pd.DataFrame(values, index=index, columns=columns)
    prepared = PreparedDataset(
        frame=frame, raw_frame=frame, segments={}, feature_names=[f"synthetic_{i}" for i in range(16)],
        coverage={}, medians={}, manifest={},
    )
    (root / "provider").mkdir()
    qlib.init(provider_uri=str(root / "provider"), expression_cache=None, dataset_cache=None)
    uri = f"sqlite:///{root / 'mlflow.db'}"
    _prepare_recorder_experiment(uri, "memory_regression", root / "mlruns")
    samples = []

    def progress(stage, _percent, details):
        if stage == "walk_forward_window_saved":
            sample = {
                "window": details["window_index"],
                "retained_windows": details["retained_training_windows"],
                **read_runtime_resources().public(),
            }
            samples.append(sample)
            print(json.dumps({"memory_sample": sample}), flush=True)

    started = time.perf_counter()
    result = _run_walk_forward(
        prepared,
        {
            "strategy": "rolling", "train_sessions": 252, "valid_sessions": 21,
            "embargo_sessions": 5, "test_sessions": 5, "step_sessions": 5,
            "oos_date_start": str(dates[283].date()), "oos_date_end": str(dates[-1].date()),
        },
        work_dir=root, model_id="synthetic_memory_check", model_version=1,
        model_kind="lightgbm",
        raw_params={"n_estimators": 3, "num_threads": threads, "early_stopping_rounds": 2},
        DataHandlerLP=DataHandlerLP, DatasetH=DatasetH,
        recorder_uri=uri, experiment_name="memory_regression", cancellation=None, progress=progress,
    )
    assert len(result.report["windows"]) == args.windows
    assert len(result.prediction) == args.windows * 5 * 256
    report = {
        "synthetic_data_only": True,
        "registered_model": False,
        "windows": args.windows,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "resources": resources.public(),
        "window_memory_samples": samples,
        "prediction_rows": len(result.prediction),
    }
    (root / "memory_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"completed": True, "report": str(root / "memory_report.json")}), flush=True)


if __name__ == "__main__":
    main()
