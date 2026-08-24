#!/usr/bin/env python3
"""Run every supported model through a small real AutoDL Pro training job."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import sys
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


BASE_URL = "http://127.0.0.1:8100/model-research"
NODE_ID = "autodl-pro-test-01"
SOURCE_JOB_ID = "model_job_dd38d029fc4e449e8cb79544a5a55130"
TERMINAL = {"succeeded", "failed", "canceled"}

MODEL_SCENARIOS: list[tuple[str, dict[str, Any]]] = [
    ("lightgbm", {
        "n_estimators": 20, "early_stopping_rounds": 5,
        "num_leaves": 15, "max_depth": 4, "num_threads": 4,
    }),
    ("xgboost", {
        "n_estimators": 20, "early_stopping_rounds": 5,
        "max_depth": 4, "num_threads": 4,
    }),
    ("catboost", {
        "n_estimators": 20, "early_stopping_rounds": 5,
        "depth": 4, "num_threads": 4,
    }),
    ("random_forest", {
        "n_estimators": 20, "max_depth": 6, "num_threads": 4,
    }),
    ("linear", {"alpha": 1.0, "max_iter": 100, "num_threads": 4}),
    ("mlp", {
        "hidden_layers": [16], "max_steps": 2, "batch_size": 256,
        "eval_steps": 1, "early_stopping_rounds": 1, "num_threads": 4,
    }),
    ("gru", {
        "lookback_window": 8, "hidden_size": 16, "num_layers": 1,
        "dropout": 0.0, "max_steps": 2, "batch_size": 256,
        "eval_steps": 1, "early_stopping_rounds": 1, "num_threads": 4,
    }),
    ("lstm", {
        "lookback_window": 8, "hidden_size": 16, "num_layers": 1,
        "dropout": 0.0, "max_steps": 2, "batch_size": 256,
        "eval_steps": 1, "early_stopping_rounds": 1, "num_threads": 4,
    }),
    ("alstm", {
        "lookback_window": 8, "hidden_size": 16, "num_layers": 1,
        "dropout": 0.0, "max_steps": 2, "batch_size": 256,
        "eval_steps": 1, "early_stopping_rounds": 1, "num_threads": 4,
    }),
    ("transformer", {
        "lookback_window": 8, "d_model": 16, "nhead": 4,
        "transformer_layers": 1, "dim_feedforward": 32, "dropout": 0.0,
        "max_steps": 2, "batch_size": 256, "eval_steps": 1,
        "early_stopping_rounds": 1, "num_threads": 4,
    }),
    ("tabnet", {
        "n_d": 8, "n_a": 8, "n_steps": 2, "n_shared": 1, "n_ind": 1,
        "batch_size": 512, "max_steps": 2, "early_stopping_rounds": 1,
        "pretrain": False, "num_threads": 4,
    }),
    ("tcn", {
        "lookback_window": 8, "hidden_size": 16, "kernel_size": 3,
        "num_layers": 2, "dropout": 0.0, "max_steps": 2,
        "batch_size": 256, "eval_steps": 1,
        "early_stopping_rounds": 1, "num_threads": 4,
    }),
    ("nativetft", {
        "lookback_window": 8, "d_model": 16, "nhead": 4,
        "gru_hidden_size": 16, "num_layers": 1, "dim_feedforward": 32,
        "dropout": 0.0, "max_steps": 2, "batch_size": 256,
        "eval_steps": 1, "early_stopping_rounds": 1, "num_threads": 4,
    }),
    ("transformer_lstm", {
        "lookback_window": 8, "d_model": 16, "nhead": 4,
        "transformer_layers": 1, "dim_feedforward": 32,
        "lstm_hidden_size": 16, "lstm_layers": 1, "dropout": 0.0,
        "max_steps": 2, "batch_size": 256, "eval_steps": 1,
        "early_stopping_rounds": 1, "num_threads": 4,
    }),
]


def api(path: str, *, method: str = "GET", payload: Any = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{BASE_URL}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc


def node_setting() -> dict[str, Any]:
    nodes = api("/execution-node-settings")["nodes"]
    return next(node for node in nodes if node["id"] == NODE_ID)


def set_auto_stop(enabled: bool) -> None:
    node = node_setting()
    node["auto_stop"] = enabled
    api(f"/execution-node-settings/{NODE_ID}", method="PUT", payload=node)


def wait_running(timeout_seconds: int = 900) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = api(f"/execution-nodes/{NODE_ID}/lifecycle")["lifecycle"]["power_state"]
        print(f"LIFECYCLE power_state={state}", flush=True)
        if state == "running":
            return
        if state in {"shutdown", "stopped"}:
            api(f"/execution-nodes/{NODE_ID}/power-on", method="POST", payload={})
        time.sleep(10)
    raise TimeoutError("AutoDL Pro instance did not reach running state")


def wait_worker_idle(timeout_seconds: int = 120) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with urlopen("http://127.0.0.1:8100/research/status", timeout=30) as response:
            status = json.load(response)
        if not status.get("busy"):
            return
        time.sleep(2)
    raise TimeoutError("research worker did not become idle")


def wait_job(job_id: str, timeout_seconds: int = 1200) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_progress = None
    while time.monotonic() < deadline:
        job = api(f"/jobs/{job_id}")["job"]
        progress = job.get("progress_json") or {}
        marker = (job["status"], progress.get("stage"), progress.get("percent"))
        if marker != last_progress:
            print(
                f"JOB {job_id} status={marker[0]} stage={marker[1]} "
                f"percent={marker[2]}", flush=True,
            )
            last_progress = marker
        if job["status"] in TERMINAL:
            return job
        time.sleep(2)
    api(f"/jobs/{job_id}/cancel", method="POST", payload={})
    raise TimeoutError(f"training timeout: {job_id}")


def smoke_dataset() -> dict[str, Any]:
    source = api(f"/jobs/{SOURCE_JOB_ID}")["job"]["config_json"]
    dataset = deepcopy(source["dataset"])
    dataset.update({
        "name": "AutoDL Pro 全模型环境冒烟数据集",
        "date_start": "2026-01-01",
        "date_end": "2026-06-01",
        "universe_id": "csi500",
        "index_code": "000905.SH",
        "benchmark_code": "000905.SH",
    })
    return dataset


def summarize(job: dict[str, Any], started: float) -> dict[str, Any]:
    result = job.get("result_json") or {}
    metrics = result.get("metrics") or {}
    manifest = result.get("manifest") or {}
    predictions = result.get("predictions") or {}
    return {
        "model_kind": job["model_kind"],
        "job_id": job["job_id"],
        "status": job["status"],
        "seconds": round(time.monotonic() - started, 1),
        "error": job.get("error_message") or "",
        "prediction_rows": predictions.get("row_count") or manifest.get("prediction_rows"),
        "auc": metrics.get("auc"),
        "ic": metrics.get("ic"),
        "accelerator": (manifest.get("environment") or {}).get("accelerator"),
        "platform": (manifest.get("environment") or {}).get("platform"),
        "registration_status": job.get("registration_status"),
    }


def power_off() -> str:
    set_auto_stop(True)
    try:
        api(f"/execution-nodes/{NODE_ID}/power-off", method="POST", payload={})
    except Exception as exc:
        print(f"POWER_OFF request_error={exc}", file=sys.stderr, flush=True)
    deadline = time.monotonic() + 300
    state = "unknown"
    while time.monotonic() < deadline:
        try:
            state = api(f"/execution-nodes/{NODE_ID}/lifecycle")["lifecycle"]["power_state"]
        except Exception as exc:
            print(f"POWER_OFF poll_error={exc}", file=sys.stderr, flush=True)
            time.sleep(5)
            continue
        print(f"POWER_OFF power_state={state}", flush=True)
        if state in {"shutdown", "stopped"}:
            return state
        time.sleep(5)
    return state


def main() -> int:
    results: list[dict[str, Any]] = []
    active_job_id = ""
    shutdown_state = "unknown"
    try:
        set_auto_stop(False)
        wait_running()
        connection = api(f"/execution-nodes/{NODE_ID}/test", method="POST", payload={})
        if not connection.get("success"):
            raise RuntimeError(f"remote node test failed: {connection}")
        dataset = smoke_dataset()
        for index, (kind, params) in enumerate(MODEL_SCENARIOS, start=1):
            wait_worker_idle()
            payload = {
                "title": f"[环境冒烟 {index:02d}/14] AutoDL Pro · {kind}",
                "dataset": dataset,
                "model": {"kind": kind, "params": params},
                "walk_forward": {
                    "enabled": False, "strategy": "rolling", "max_windows": 2,
                    "step_months": 6, "test_months": 6, "train_years": 1,
                    "valid_months": 3, "embargo_days": 5,
                },
                "execution": {"mode": "remote_ssh_docker", "node_id": NODE_ID},
            }
            created = api("/jobs", method="POST", payload=payload)["job"]
            active_job_id = created["job_id"]
            print(f"START {index:02d}/14 model={kind} job_id={active_job_id}", flush=True)
            started = time.monotonic()
            api(f"/jobs/{active_job_id}/dispatch", method="POST", payload={})
            completed = wait_job(active_job_id)
            summary = summarize(completed, started)
            results.append(summary)
            print("RESULT " + json.dumps(summary, ensure_ascii=False), flush=True)
            active_job_id = ""
    except BaseException:
        if active_job_id:
            try:
                job = api(f"/jobs/{active_job_id}")["job"]
                if job["status"] not in TERMINAL:
                    api(f"/jobs/{active_job_id}/cancel", method="POST", payload={})
                    wait_job(active_job_id, timeout_seconds=120)
            except Exception as exc:
                print(f"CANCEL job_id={active_job_id} error={exc}", file=sys.stderr, flush=True)
        raise
    finally:
        try:
            wait_worker_idle(timeout_seconds=180)
        except Exception as exc:
            print(f"IDLE_WAIT error={exc}", file=sys.stderr, flush=True)
        shutdown_state = power_off()
        print("FINAL " + json.dumps({
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "model_count": len(MODEL_SCENARIOS),
            "completed_count": len(results),
            "succeeded_count": sum(item["status"] == "succeeded" for item in results),
            "failed_count": sum(item["status"] != "succeeded" for item in results),
            "shutdown_state": shutdown_state,
            "results": results,
        }, ensure_ascii=False), flush=True)
    return 0 if all(item["status"] == "succeeded" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
