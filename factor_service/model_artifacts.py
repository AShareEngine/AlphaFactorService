from __future__ import annotations

from contextlib import contextmanager
import fcntl
from hashlib import sha256
from pathlib import Path
import os
import re
import shutil
import tempfile
import time
from typing import BinaryIO, Iterator


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_DATASET_HASH = re.compile(r"^[0-9a-f]{64}$")
_DATASET_SCOPED_KINDS = {"dataset", "dataset_raw", "dataset_manifest"}
_DATASET_LAST_USED_FILE = ".last_used"


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
        relative = Path(str(relative_path))
        parts = relative.parts
        if (
            len(parts) >= 3
            and parts[0] == "datasets"
            and _DATASET_HASH.fullmatch(parts[1])
        ):
            dataset_hash = parts[1]
            with self.dataset_lock(dataset_hash):
                target = self._resolve_file(relative)
                self.touch_dataset(dataset_hash)
                return target
        return self._resolve_file(relative)

    def object_cache_path(self, digest: str, file_name: str) -> Path:
        """Return a content-addressed cache target for a remote model artifact."""
        clean_digest = str(digest or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", clean_digest):
            raise ArtifactError("模型对象缓存缺少有效SHA256")
        safe_file = self._name(file_name, "file_name")
        target = (self.root / ".object-cache" / clean_digest / safe_file).resolve()
        if self.root not in target.parents:
            raise ArtifactError("模型对象缓存路径越界")
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
        """Delete one local dataset snapshot without racing an active reader."""

        clean_hash = self._dataset_hash(dataset_hash)
        with self.dataset_lock(clean_hash):
            return self._remove_directory(
                (self.root / "datasets" / clean_hash).resolve(),
            )

    @contextmanager
    def dataset_lock(
        self, dataset_hash: str, *, blocking: bool = True,
    ) -> Iterator[None]:
        """Serialize dataset publication, reuse, and expiry across processes."""

        clean_hash = self._dataset_hash(dataset_hash)
        lock_root = (self.root / ".dataset-locks").resolve()
        lock_root.mkdir(parents=True, exist_ok=True)
        lock_path = lock_root / f"{clean_hash}.lock"
        with lock_path.open("a+b") as descriptor:
            operation = fcntl.LOCK_EX
            if not blocking:
                operation |= fcntl.LOCK_NB
            fcntl.flock(descriptor.fileno(), operation)
            try:
                yield
            finally:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)

    def touch_dataset(self, dataset_hash: str, *, used_at: float | None = None) -> Path:
        """Record successful materialization or reuse of a local dataset cache."""

        clean_hash = self._dataset_hash(dataset_hash)
        dataset_dir = (self.root / "datasets" / clean_hash).resolve()
        if not dataset_dir.is_dir():
            raise ArtifactError("训练数据集缓存不存在")
        marker = dataset_dir / _DATASET_LAST_USED_FILE
        marker.touch(exist_ok=True)
        timestamp = time.time() if used_at is None else float(used_at)
        os.utime(marker, (timestamp, timestamp))
        return marker

    def prune_dataset_cache(
        self,
        *,
        retention_seconds: float = 24 * 60 * 60,
        protected_hashes: set[str] | frozenset[str] = frozenset(),
        now: float | None = None,
    ) -> dict[str, object]:
        """Remove local dataset snapshots not used within the retention window."""

        retention = float(retention_seconds)
        if retention <= 0:
            raise ArtifactError("训练数据集缓存保留时间必须大于0")
        protected = {
            self._dataset_hash(value) for value in protected_hashes
        }
        cutoff = (time.time() if now is None else float(now)) - retention
        datasets_root = (self.root / "datasets").resolve()
        result: dict[str, object] = {
            "scanned": 0,
            "deleted": [],
            "reclaimed_bytes": 0,
            "protected": 0,
            "locked": 0,
        }
        if not datasets_root.is_dir():
            return result

        for directory in sorted(datasets_root.iterdir()):
            if not directory.is_dir() or not _DATASET_HASH.fullmatch(directory.name):
                continue
            result["scanned"] = int(result["scanned"]) + 1
            dataset_hash = directory.name
            if dataset_hash in protected:
                result["protected"] = int(result["protected"]) + 1
                continue
            try:
                with self.dataset_lock(dataset_hash, blocking=False):
                    if not directory.is_dir():
                        continue
                    if self._dataset_last_used_at(directory) > cutoff:
                        continue
                    size_bytes = sum(
                        path.stat().st_size
                        for path in directory.rglob("*")
                        if path.is_file()
                    )
                    self._remove_directory(directory.resolve())
                    deleted = result["deleted"]
                    assert isinstance(deleted, list)
                    deleted.append(dataset_hash)
                    result["reclaimed_bytes"] = (
                        int(result["reclaimed_bytes"]) + size_bytes
                    )
            except BlockingIOError:
                result["locked"] = int(result["locked"]) + 1
        return result

    @staticmethod
    def _dataset_last_used_at(directory: Path) -> float:
        marker = directory / _DATASET_LAST_USED_FILE
        if marker.is_file():
            return marker.stat().st_mtime
        timestamps = [directory.stat().st_mtime]
        timestamps.extend(
            path.stat().st_mtime for path in directory.rglob("*") if path.is_file()
        )
        return max(timestamps)

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

    def _resolve_file(self, relative_path: str | Path) -> Path:
        target = (self.root / relative_path).resolve()
        if self.root not in target.parents or not target.is_file():
            raise ArtifactError("模型产物不存在")
        return target

    @staticmethod
    def _name(value: str, field: str) -> str:
        clean = str(value or "").strip()
        if not _SAFE_NAME.fullmatch(clean) or clean in {".", ".."}:
            raise ArtifactError(f"{field}包含非法字符")
        return clean

    @staticmethod
    def _dataset_hash(value: str) -> str:
        clean = str(value or "").strip().lower()
        if not _DATASET_HASH.fullmatch(clean):
            raise ArtifactError("dataset_hash无效")
        return clean


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["ArtifactError", "ModelArtifactStore"]
