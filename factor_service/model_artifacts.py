from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import os
import re
import shutil
import tempfile
from typing import BinaryIO


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_DATASET_SCOPED_KINDS = {"dataset", "dataset_raw", "dataset_manifest"}


class ArtifactError(ValueError):
    pass


class ModelArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(
        self, *, job_id: str, artifact_kind: str, file_name: str,
        source: BinaryIO, expected_sha256: str, dataset_hash: str = "",
        max_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> dict[str, object]:
        safe_job = self._name(job_id, "job_id")
        safe_kind = self._name(artifact_kind, "artifact_kind")
        safe_file = self._name(file_name, "file_name")
        expected = str(expected_sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ArtifactError("X-Artifact-SHA256必须是64位十六进制摘要")
        if safe_kind in _DATASET_SCOPED_KINDS:
            clean_dataset_hash = str(dataset_hash or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", clean_dataset_hash):
                raise ArtifactError("数据集产物缺少有效dataset_hash")
            destination_dir = (self.root / "datasets" / clean_dataset_hash).resolve()
        else:
            destination_dir = (self.root / safe_job / safe_kind).resolve()
        if self.root not in destination_dir.parents:
            raise ArtifactError("产物路径越界")
        destination_dir.mkdir(parents=True, exist_ok=True)
        digest = sha256()
        size = 0
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{safe_file}.", suffix=".upload", dir=destination_dir,
        )
        try:
            with os.fdopen(descriptor, "wb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise ArtifactError("模型产物超过2GiB限制")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            actual = digest.hexdigest()
            if actual != expected:
                raise ArtifactError("模型产物SHA256校验失败")
            destination = destination_dir / safe_file
            if destination.exists():
                existing = _file_sha256(destination)
                if existing != actual and safe_kind in _DATASET_SCOPED_KINDS:
                    raise ArtifactError("相同dataset_hash的冻结数据集内容不一致")
                if existing == actual:
                    Path(temporary_name).unlink(missing_ok=True)
                else:
                    # A retry of the same job may produce a different manifest,
                    # recorder database, or prediction timestamp. Non-dataset
                    # artifacts are job-scoped and can be atomically replaced.
                    os.replace(temporary_name, destination)
            else:
                os.replace(temporary_name, destination)
            relative = destination.relative_to(self.root).as_posix()
            return {
                "relative_path": relative,
                "sha256": actual,
                "size_bytes": size,
                "path": destination,
            }
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    def publish_file(
        self,
        *,
        job_id: str,
        artifact_kind: str,
        source_path: str | Path,
        dataset_hash: str = "",
    ) -> dict[str, object]:
        path = Path(source_path)
        if not path.is_file():
            raise ArtifactError(f"模型产物不存在: {path}")
        digest = _file_sha256(path)
        with path.open("rb") as source:
            return self.save(
                job_id=job_id,
                artifact_kind=artifact_kind,
                file_name=path.name,
                source=source,
                expected_sha256=digest,
                dataset_hash=dataset_hash,
            )

    def resolve(self, relative_path: str) -> Path:
        target = (self.root / str(relative_path)).resolve()
        if self.root not in target.parents or not target.is_file():
            raise ArtifactError("模型产物不存在")
        return target

    def delete_job_artifacts(self, job_id: str) -> dict[str, bool]:
        """Delete only artifacts owned by one model job.

        Dataset snapshots live under ``datasets/{dataset_hash}`` and may be
        shared by multiple jobs, so this method deliberately leaves them in
        place.  The caller can remove an unreferenced dataset separately.
        """

        safe_job = self._name(job_id, "job_id")
        job_directory = (self.root / safe_job).resolve()
        upload_directory = (self.root / ".uploads" / safe_job).resolve()
        removed = {
            "job_artifacts": self._remove_directory(job_directory),
            "pending_uploads": self._remove_directory(upload_directory),
        }
        uploads_root = (self.root / ".uploads").resolve()
        try:
            uploads_root.rmdir()
        except OSError:
            pass
        return removed

    def delete_dataset_artifacts(self, dataset_hash: str) -> bool:
        """Delete an immutable dataset snapshot after its final DB reference is gone."""

        clean_hash = str(dataset_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", clean_hash):
            raise ArtifactError("dataset_hash无效")
        return self._remove_directory(
            (self.root / "datasets" / clean_hash).resolve(),
        )

    def save_chunk(
        self, *, job_id: str, artifact_kind: str, file_name: str,
        upload_id: str, chunk_index: int, chunk_sha256: str, source: BinaryIO,
        max_chunk_bytes: int = 16 * 1024 * 1024,
    ) -> dict[str, object]:
        directory = self._upload_dir(job_id, artifact_kind, file_name, upload_id)
        index = int(chunk_index)
        if index < 0 or index > 1_000_000:
            raise ArtifactError("分片序号无效")
        expected = str(chunk_sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ArtifactError("X-Chunk-SHA256必须是64位十六进制摘要")
        body = source.read(max_chunk_bytes + 1)
        if len(body) > max_chunk_bytes:
            raise ArtifactError("单个模型产物分片超过16MiB限制")
        actual = sha256(body).hexdigest()
        if actual != expected:
            raise ArtifactError("模型产物分片SHA256校验失败")
        directory.mkdir(parents=True, exist_ok=True)
        if index == 0:
            # Worker uses a deterministic upload id. Starting again at chunk zero
            # means the previous transfer was interrupted, so discard its temp parts.
            for stale in directory.glob("*.part"):
                stale.unlink(missing_ok=True)
        destination = directory / f"{index:08d}.part"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".chunk.", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(body)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary_name, destination)
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise
        return {"chunk_index": index, "size_bytes": len(body), "sha256": actual}

    def assemble_chunks(
        self, *, job_id: str, artifact_kind: str, file_name: str,
        upload_id: str, total_chunks: int, expected_sha256: str,
        dataset_hash: str = "",
    ) -> dict[str, object]:
        directory = self._upload_dir(job_id, artifact_kind, file_name, upload_id)
        total = int(total_chunks)
        if total <= 0 or total > 1_000_000:
            raise ArtifactError("总分片数无效")
        paths = [directory / f"{index:08d}.part" for index in range(total)]
        if any(not path.is_file() for path in paths):
            raise ArtifactError("模型产物分片不完整")
        descriptor, assembled_name = tempfile.mkstemp(prefix=".assembled.", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as target:
                for path in paths:
                    with path.open("rb") as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            target.write(chunk)
            with open(assembled_name, "rb") as source:
                return self.save(
                    job_id=job_id, artifact_kind=artifact_kind, file_name=file_name,
                    source=source, expected_sha256=expected_sha256,
                    dataset_hash=dataset_hash,
                )
        finally:
            Path(assembled_name).unlink(missing_ok=True)
            for path in paths:
                path.unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                pass

    def _upload_dir(self, job_id: str, artifact_kind: str, file_name: str, upload_id: str) -> Path:
        safe_job = self._name(job_id, "job_id")
        safe_kind = self._name(artifact_kind, "artifact_kind")
        safe_file = self._name(file_name, "file_name")
        safe_upload = self._name(upload_id, "upload_id")
        directory = (self.root / ".uploads" / safe_job / safe_kind / safe_file / safe_upload).resolve()
        if self.root not in directory.parents:
            raise ArtifactError("分片上传路径越界")
        return directory

    def _remove_directory(self, directory: Path) -> bool:
        if self.root not in directory.parents:
            raise ArtifactError("产物路径越界")
        if not directory.exists():
            return False
        if not directory.is_dir():
            raise ArtifactError("模型产物目录无效")
        shutil.rmtree(directory)
        return True

    @staticmethod
    def _name(value: str, field: str) -> str:
        clean = str(value or "").strip()
        if not _SAFE_NAME.fullmatch(clean) or clean in {".", ".."}:
            raise ArtifactError(f"{field}包含非法字符")
        return clean


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["ArtifactError", "ModelArtifactStore"]
