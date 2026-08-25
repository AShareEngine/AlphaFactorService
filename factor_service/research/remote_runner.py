from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from typing import Any

from factor_service.research.config import Settings
from factor_service.research.job import validate_job
from factor_service.research.trainer import QlibTrainer, TrainingResult


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train one AlphaFactorService model from a transferred immutable dataset",
    )
    parser.add_argument("job_path", type=Path)
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("result_path", type=Path)
    parser.add_argument("artifact_root", type=Path)
    parser.add_argument("progress_path", type=Path)
    args = parser.parse_args()

    job = validate_job(json.loads(args.job_path.read_text(encoding="utf-8")))
    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.artifact_root.mkdir(parents=True, exist_ok=True)
    args.progress_path.parent.mkdir(parents=True, exist_ok=True)
    args.progress_path.unlink(missing_ok=True)

    settings = Settings(
        # The immutable snapshot must already exist.  These values are deliberately
        # unusable so a remote node can never silently query a different data source.
        clickhouse_host="127.0.0.1",
        clickhouse_port=1,
        clickhouse_user="remote-snapshot-only",
        clickhouse_password="",
        factor_database="ab_factor",
        model_database="ab_model",
        source_database="starlight",
        work_root=args.work_dir.parent,
        model_artifacts_root=args.artifact_root,
        scheduler_enabled=False,
        scheduler_refresh_seconds=60,
    )

    started_at = time.perf_counter()
    stage_started_at: dict[str, float] = {}

    def progress(stage: str, percent: int, details: dict[str, Any]) -> None:
        now = time.perf_counter()
        stage_started_at.setdefault(str(stage), now)
        payload = {
            "stage": str(stage),
            "percent": max(0, min(int(percent), 100)),
            "details": {
                **dict(details or {}),
                "elapsed_seconds": round(now - started_at, 3),
                "stage_elapsed_seconds": round(
                    now - stage_started_at[str(stage)], 3,
                ),
                "effective_num_threads": int(
                    os.environ.get("ALPHA_EFFECTIVE_NUM_THREADS") or 0
                ),
                "accelerator": str(
                    os.environ.get("ALPHA_MODEL_ACCELERATOR") or "cpu"
                ),
            },
        }
        with args.progress_path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            target.flush()
            os.fsync(target.fileno())

    progress("remote_runtime_initialized", 1, {
        "job_id": str(job.get("job_id") or ""),
        "model_kind": str((job.get("config_json") or {}).get("model", {}).get("kind") or ""),
    })

    result = QlibTrainer(settings).train(job, args.work_dir, progress=progress)
    _write_result(
        args.result_path,
        result,
        work_dir=args.work_dir,
        artifact_root=args.artifact_root,
    )
    progress("remote_packaged", 89, {"artifact_count": len(result.artifacts)})


def _write_result(
    path: Path,
    result: TrainingResult,
    *,
    work_dir: Path,
    artifact_root: Path,
) -> None:
    payload = {
        "schema_version": "alphablocks.remote-training-result.v1",
        "result": result.result,
        "artifacts": [
            {"kind": str(kind), **_scoped_path(artifact, work_dir, artifact_root)}
            for kind, artifact in result.artifacts
        ],
        "predictions": _scoped_path(result.predictions_path, work_dir, artifact_root),
    }
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _scoped_path(path: Path, work_dir: Path, artifact_root: Path) -> dict[str, str]:
    # Normalize ``..`` without dereferencing symlinks. Remote immutable datasets
    # deliberately live in a shared cache and are mounted into artifact_root via
    # a symlink; resolving that link would incorrectly classify them as escaped.
    # The local loader applies its own containment check before consuming paths.
    scoped = Path(os.path.abspath(path))
    work = Path(os.path.abspath(work_dir))
    artifacts = Path(os.path.abspath(artifact_root))
    if scoped == work or work in scoped.parents:
        return {"scope": "work", "path": scoped.relative_to(work).as_posix()}
    if scoped == artifacts or artifacts in scoped.parents:
        return {
            "scope": "artifact_root",
            "path": scoped.relative_to(artifacts).as_posix(),
        }
    raise ValueError(f"远程训练产物路径不属于允许目录: {scoped}")


if __name__ == "__main__":
    main()
