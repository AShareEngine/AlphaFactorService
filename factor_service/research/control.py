from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import shutil
from typing import Any, Callable, TypeVar

from psycopg import OperationalError

from factor_service.model_artifacts import ModelArtifactStore
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
    ) -> None:
        self.repository = repository
        self.artifact_store = artifact_store

    def check(self) -> dict[str, Any]:
        self._call(self.repository.list_jobs, limit=1)
        return {"ok": True, "reachable": True}

    def download_artifact(
        self, artifact_id: str, destination: Path, expected_sha256: str,
    ) -> Path:
        artifact = self._call(self.repository.get_artifact, artifact_id)
        source = self.artifact_store.resolve(str(artifact["relative_path"]))
        actual = _file_sha256(source)
        if actual != str(expected_sha256).lower() or actual != str(artifact["sha256"]).lower():
            raise ResearchControlError(
                "模型产物SHA256不一致", retryable=False, code="artifact_hash_mismatch",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            shutil.copyfile(source, temporary)
            temporary.replace(destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

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
