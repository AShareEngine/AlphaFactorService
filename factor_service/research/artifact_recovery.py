from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
import threading
import time
import traceback
from typing import Any

from factor_service.model_artifacts import ModelArtifactStore
from factor_service.model_object_store import ModelObjectStore
from factor_service.model_research_repository import ModelResearchRepository
from factor_service.research.config import Settings, load_settings
from factor_service.research.control import ResearchControl, ResearchControlError
from factor_service.research.errors import classify_exception
from factor_service.research.job import CancellationToken, safe_job_dir
from factor_service.research.remote import _load_remote_result
from factor_service.research.trainer import QlibTrainer, TrainingResult


def recover_training_artifacts(
    job_id: str,
    *,
    source_attempt: int,
    settings: Settings | None = None,
    repository: ModelResearchRepository | None = None,
) -> dict[str, Any]:
    """Publish a verified remote result without rerunning model training."""
    active_settings = settings or load_settings()
    active_repository = repository or ModelResearchRepository()
    artifact_store = ModelArtifactStore(active_settings.model_artifacts_root)
    object_store = ModelObjectStore(active_settings.model_object_store)
    control = ResearchControl(active_repository, artifact_store, object_store)

    current = active_repository.get_job(job_id)
    source_ordinal = int(source_attempt)
    work_dir = safe_job_dir(active_settings.work_root, job_id) / (
        f"attempt-{source_ordinal:03d}"
    )
    trained = _load_remote_result(
        work_dir / "remote_result.json",
        work_dir,
        active_settings.model_artifacts_root,
    )
    recovery_evidence = _validate_recovery_identity(
        current,
        trained,
        source_attempt=source_ordinal,
        result_path=work_dir / "remote_result.json",
    )
    print(json.dumps({
        "event": "artifact_recovery_preflight_complete",
        **recovery_evidence,
    }, ensure_ascii=False), flush=True)

    job = active_repository.claim_artifact_recovery(
        job_id,
        source_attempt=source_ordinal,
        lease_seconds=300,
    )
    lease_token = str(job["lease_token"])
    cancellation = CancellationToken(timeout_seconds=30 * 60)
    monitor_stop = threading.Event()
    progress_lock = threading.Lock()
    progress: dict[str, Any] = {
        "stage": "artifact_recovery",
        "percent": 90,
        "source_attempt": source_ordinal,
    }
    monitor = threading.Thread(
        target=_monitor_recovery_lease,
        args=(
            control,
            job_id,
            lease_token,
            cancellation,
            monitor_stop,
            progress,
            progress_lock,
        ),
        name=f"artifact-recovery-{job_id[-12:]}",
        daemon=True,
    )
    monitor.start()
    try:
        control.stage(job_id, lease_token, "uploading", progress)
        remote_artifacts: list[dict[str, object]] = []
        artifact_count = max(1, len(trained.artifacts))
        for artifact_index, (kind, path) in enumerate(trained.artifacts):
            cancellation.checkpoint()
            saved = artifact_store.publish_file(
                job_id=job_id,
                artifact_kind=kind,
                source_path=path,
                dataset_hash=str(job.get("dataset_hash") or ""),
            )
            remote = None
            if object_store.enabled_for(kind):
                remote = object_store.publish_file(
                    job_id=job_id,
                    model_id=str(job["model_id"]),
                    model_version=int(
                        job.get("model_version")
                        or (job.get("config_json") or {}).get(
                            "planned_model_version"
                        )
                        or 0
                    ),
                    artifact_kind=kind,
                    source_path=saved["path"],
                    digest=str(saved["sha256"]),
                    size_bytes=int(saved["size_bytes"]),
                )
                if remote is not None:
                    remote_artifacts.append(dict(remote))
            control.record_artifact(
                job_id,
                lease_token,
                kind=kind,
                file_name=path.name,
                relative_path=str(saved["relative_path"]),
                digest=str(saved["sha256"]),
                size_bytes=int(saved["size_bytes"]),
                dataset_hash=str(job.get("dataset_hash") or ""),
                object_store_uri=str((remote or {}).get("object_uri") or ""),
                object_store_version_id=str((remote or {}).get("version_id") or ""),
                object_store_sha256=str((remote or {}).get("sha256") or ""),
            )
            with progress_lock:
                progress.update({
                    "stage": "artifact_recovery",
                    "percent": min(
                        96,
                        90 + int(6 * (artifact_index + 1) / artifact_count),
                    ),
                    "artifact": kind,
                    "artifact_index": artifact_index + 1,
                    "artifact_count": artifact_count,
                })
                snapshot = dict(progress)
            control.renew(
                job_id,
                lease_token,
                snapshot,
                record_event=True,
            )
            print(json.dumps({
                "event": "artifact_recovered",
                "artifact_kind": kind,
                "file_name": path.name,
                "size_bytes": int(saved["size_bytes"]),
                "sha256": str(saved["sha256"]),
                "object_store_uri": str((remote or {}).get("object_uri") or ""),
                "object_store_version_id": str(
                    (remote or {}).get("version_id") or ""
                ),
            }, ensure_ascii=False), flush=True)

        if remote_artifacts:
            trained.result["object_storage"] = {
                **object_store.public_config(),
                "artifacts": remote_artifacts,
            }
        trained.result["artifact_recovery"] = {
            **recovery_evidence,
            "schema_version": "alphablocks.model-artifact-recovery.v1",
            "status": "completed",
            "recovery_attempt": int(job.get("attempt_count") or 0),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        with progress_lock:
            progress.update({
                "stage": "publishing_predictions",
                "percent": 97,
            })
            snapshot = dict(progress)
        control.renew(job_id, lease_token, snapshot, record_event=True)
        publisher = QlibTrainer(active_settings)
        inserted = publisher.publish_predictions(
            trained.predictions_path,
            job,
            cancellation=cancellation,
        )
        expected = int(trained.result["predictions"]["row_count"])
        if inserted != expected:
            raise ValueError(
                f"预测发布行数不一致: 期望{expected}，实际{inserted}"
            )
        with progress_lock:
            progress.update({
                "stage": "completing",
                "percent": 99,
                "prediction_rows": inserted,
            })
            snapshot = dict(progress)
        control.renew(job_id, lease_token, snapshot, record_event=True)
        response = control.complete(job_id, lease_token, trained.result)
        completed = dict(response.get("job") or {})
        print(json.dumps({
            "event": "artifact_recovery_complete",
            "job_id": job_id,
            "status": completed.get("status"),
            "model_id": completed.get("model_id"),
            "model_version": completed.get("model_version"),
            "prediction_rows": inserted,
        }, ensure_ascii=False), flush=True)
        return completed
    except Exception as exc:
        latest = active_repository.get_job(job_id)
        if str(latest.get("status") or "") == "succeeded":
            print(json.dumps({
                "event": "artifact_recovery_registration_pending",
                "job_id": job_id,
                "status": "succeeded",
                "error": str(exc),
            }, ensure_ascii=False), flush=True)
            return latest
        retryable, code = classify_exception(exc)
        error = (
            f"[{code}] {type(exc).__name__}: {exc}\n"
            f"{traceback.format_exc()}"
        )
        try:
            control.fail(
                job_id,
                lease_token,
                error,
                retryable=False,
            )
        except Exception as report_error:
            print(
                f"产物恢复失败状态回传失败: {report_error}",
                file=sys.stderr,
                flush=True,
            )
        raise RuntimeError(
            f"产物恢复入库失败({code}, retryable={retryable}): {exc}"
        ) from exc
    finally:
        monitor_stop.set()
        monitor.join(timeout=6)


def _validate_recovery_identity(
    job: dict[str, Any],
    trained: TrainingResult,
    *,
    source_attempt: int,
    result_path: Path,
) -> dict[str, Any]:
    if str(job.get("kind") or "train") != "train":
        raise ValueError("只有训练任务可以恢复产物入库")
    if str(job.get("status") or "") not in {"failed", "canceled"}:
        raise ValueError("任务不是可恢复的失败或已取消状态")
    attempts = {
        int(item.get("ordinal") or 0): dict(item)
        for item in job.get("attempts") or []
    }
    source = attempts.get(int(source_attempt))
    if source is None:
        raise ValueError("恢复来源Attempt不存在")
    if str(source.get("status") or "") not in {"failed", "canceled"}:
        raise ValueError("恢复来源Attempt尚未终止")
    planned_version = int(
        (job.get("config_json") or {}).get("planned_model_version") or 0
    )
    predictions = dict(trained.result.get("predictions") or {})
    if int(predictions.get("model_version") or 0) != planned_version:
        raise ValueError("恢复结果模型版本与任务预留版本不一致")
    manifest = dict(trained.result.get("manifest") or {})
    manifest_model = str(manifest.get("model_id") or "")
    if manifest_model and manifest_model != str(job.get("model_id") or ""):
        raise ValueError("恢复结果模型ID与任务不一致")
    manifest_dataset = str(manifest.get("dataset_hash") or "")
    if manifest_dataset and manifest_dataset != str(job.get("dataset_hash") or ""):
        raise ValueError("恢复结果数据集哈希与任务不一致")
    artifact_evidence = []
    for kind, path in trained.artifacts:
        artifact_evidence.append({
            "kind": str(kind),
            "file_name": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": _file_sha256(path),
        })
    return {
        "source_attempt": int(source_attempt),
        "source_result_sha256": _file_sha256(result_path),
        "model_id": str(job.get("model_id") or ""),
        "planned_model_version": planned_version,
        "dataset_hash": str(job.get("dataset_hash") or ""),
        "prediction_rows": int(predictions.get("row_count") or 0),
        "artifacts": artifact_evidence,
    }


def _monitor_recovery_lease(
    control: ResearchControl,
    job_id: str,
    lease_token: str,
    cancellation: CancellationToken,
    stop: threading.Event,
    progress: dict[str, Any],
    progress_lock: threading.Lock,
) -> None:
    next_renewal = time.monotonic()
    while not stop.wait(5):
        try:
            state = control.control(job_id, lease_token)
            if bool(state.get("cancel_requested")):
                cancellation.cancel("用户已请求取消产物恢复")
                return
            if str(state.get("status") or "") not in {
                "leased", "running", "uploading",
            }:
                cancellation.cancel("产物恢复租约已失效")
                return
            if time.monotonic() >= next_renewal:
                with progress_lock:
                    snapshot = dict(progress)
                control.renew(job_id, lease_token, snapshot)
                next_renewal = time.monotonic() + 30
        except ResearchControlError as exc:
            if not exc.retryable:
                cancellation.cancel("产物恢复租约已失效")
                return
            print(f"产物恢复租约暂时不可用: {exc}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"产物恢复租约检查失败: {exc}", file=sys.stderr, flush=True)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="恢复已完成但下载中断的远程训练产物并入库",
    )
    parser.add_argument("job_id")
    parser.add_argument("--source-attempt", type=int, required=True)
    args = parser.parse_args()
    result = recover_training_artifacts(
        args.job_id,
        source_attempt=args.source_attempt,
    )
    print(json.dumps({"ok": True, "job": result}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

