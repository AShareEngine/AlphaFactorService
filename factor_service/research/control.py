from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import shutil
from typing import Any, Callable, TypeVar

from psycopg import OperationalError

from factor_service.model_artifacts import ArtifactError, ModelArtifactStore
from factor_service.model_object_store import ModelObjectStore
from factor_service.model_research_repository import (
    ModelResearchConflict,
    ModelResearchError,
    ModelResearchNotFound,
    ModelResearchRepository,
)
from factor_service.research.errors import JobError


T = TypeVar("T")


class ResearchControlError(JobError):
    def __init__(self, message: str, *, retryable: bool, code: str) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)
        self.code = code


class ResearchControl:
    """Local model control plane backed by PostgreSQL and artifact storage."""

    def __init__(
        self,
        repository: ModelResearchRepository,
        artifact_store: ModelArtifactStore,
        model_object_store: ModelObjectStore | None = None,
    ) -> None:
        self.repository = repository
        self.artifact_store = artifact_store
        self.model_object_store = model_object_store or ModelObjectStore()

    def check(self) -> dict[str, Any]:
        self._call(self.repository.list_jobs, limit=1)
        return {"ok": True, "reachable": True}

    def download_artifact(
        self, artifact_id: str, destination: Path, expected_sha256: str,
    ) -> Path:
        source = self.resolve_artifact(artifact_id, expected_sha256)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            shutil.copyfile(source, temporary)
            temporary.replace(destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    def resolve_artifact(
        self,
        artifact_id: str,
        expected_sha256: str = "",
    ) -> Path:
        """Resolve a verified local artifact, hydrating it from MinIO when absent."""
        artifact = self._call(self.repository.get_artifact, artifact_id)
        recorded = str(artifact["sha256"] or "").strip().lower()
        expected = str(expected_sha256 or recorded).strip().lower()
        if expected != recorded:
            raise ResearchControlError(
                "模型产物SHA256与模型清单不一致",
                retryable=False,
                code="artifact_hash_mismatch",
            )
        try:
            source = self.artifact_store.resolve(str(artifact["relative_path"]))
        except ArtifactError:
            source = self._download_object_store_artifact(artifact, recorded)
        actual = _file_sha256(source)
        if actual != expected:
            raise ResearchControlError(
                "模型产物SHA256不一致", retryable=False, code="artifact_hash_mismatch",
            )
        return source

    def _download_object_store_artifact(
        self,
        artifact: dict[str, Any],
        expected_sha256: str,
    ) -> Path:
        object_uri = str(artifact.get("object_store_uri") or "").strip()
        remote_sha256 = str(
            artifact.get("object_store_sha256") or ""
        ).strip().lower()
        if not object_uri or remote_sha256 != expected_sha256:
            raise ResearchControlError(
                "本机缺少模型制品，PostgreSQL中也没有可校验的MinIO对象地址",
                retryable=False,
                code="artifact_remote_identity_missing",
            )
        cache_path = self.artifact_store.object_cache_path(
            expected_sha256,
            str(artifact.get("file_name") or "model-artifact.bin"),
        )
        if cache_path.is_file() and _file_sha256(cache_path) == expected_sha256:
            return cache_path
        return self.model_object_store.download_file(
            object_uri=object_uri,
            version_id=str(artifact.get("object_store_version_id") or ""),
            destination=cache_path,
            digest=expected_sha256,
            size_bytes=int(artifact.get("size_bytes") or 0),
        )

    def renew(
        self, job_id: str, lease_token: str, progress: dict[str, Any],
        *, record_event: bool = False,
    ) -> dict[str, Any]:
        job = self._call(
            self.repository.renew_lease,
            job_id,
            lease_token=lease_token,
            lease_seconds=90,
            progress=progress,
            record_event=record_event,
        )
        return {"ok": True, "job": job}

    def control(self, job_id: str, lease_token: str) -> dict[str, Any]:
        job = self._call(
            self.repository.worker_control, job_id, lease_token=lease_token,
        )
        return {
            "status": job.get("status"),
            "cancel_requested": bool(job.get("cancel_requested")),
            "lease_expires_at": job.get("lease_expires_at"),
        }

    def stage(
        self, job_id: str, lease_token: str, stage: str, progress: dict[str, Any],
    ) -> dict[str, Any]:
        job = self._call(
            self.repository.set_worker_stage,
            job_id,
            lease_token=lease_token,
            stage=stage,
            progress=progress,
        )
        return {"ok": True, "job": job}

    def record_artifact(
        self,
        job_id: str,
        lease_token: str,
        *,
        kind: str,
        file_name: str,
        relative_path: str,
        digest: str,
        size_bytes: int,
        dataset_hash: str = "",
        object_store_uri: str = "",
        object_store_version_id: str = "",
        object_store_sha256: str = "",
    ) -> dict[str, Any]:
        self._call(self.repository.worker_control, job_id, lease_token=lease_token)
        artifact = self._call(
            self.repository.record_artifact,
            job_id=job_id,
            artifact_kind=kind,
            file_name=file_name,
            relative_path=relative_path,
            digest=digest,
            size_bytes=size_bytes,
            dataset_hash=dataset_hash,
            object_store_uri=object_store_uri,
            object_store_version_id=object_store_version_id,
            object_store_sha256=object_store_sha256,
        )
        return {"ok": True, "artifact": artifact}

    def complete(
        self, job_id: str, lease_token: str, result: dict[str, Any],
    ) -> dict[str, Any]:
        job = self._call(
            self.repository.complete_job,
            job_id,
            lease_token=lease_token,
            result=result,
        )
        if str(job.get("kind") or "train") == "train":
            job = self._call(self.repository.finalize_training_result, job_id)
        return {"ok": True, "job": job}

    def fail(
        self,
        job_id: str,
        lease_token: str,
        error: str,
        retryable: bool = True,
    ) -> dict[str, Any]:
        job = self._call(
            self.repository.fail_job,
            job_id,
            lease_token=lease_token,
            error_message=error,
            retryable=retryable,
        )
        return {"ok": True, "job": job}

    @staticmethod
    def _call(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        try:
            return function(*args, **kwargs)
        except ResearchControlError:
            raise
        except OperationalError as exc:
            raise ResearchControlError(
                str(exc), retryable=True, code="control_database_transient",
            ) from exc
        except ModelResearchNotFound as exc:
            raise ResearchControlError(
                str(exc), retryable=False, code="model_research_not_found",
            ) from exc
        except ModelResearchConflict as exc:
            raise ResearchControlError(
                str(exc), retryable=False, code="model_research_conflict",
            ) from exc
        except ModelResearchError as exc:
            raise ResearchControlError(
                str(exc), retryable=False, code="model_research_rejected",
            ) from exc


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["ResearchControl", "ResearchControlError"]
