from hashlib import sha256
from io import BytesIO

import pytest

from factor_service.model_artifacts import ArtifactError, ModelArtifactStore


def test_artifact_publish_is_hashed_atomic_and_deduplicates_dataset(tmp_path) -> None:
    store = ModelArtifactStore(tmp_path / "artifacts")
    body = b"immutable parquet fixture"
    source_path = tmp_path / "dataset.parquet"
    source_path.write_bytes(body)
    dataset_hash = "b" * 64

    first = store.publish_file(
        job_id="job-1", artifact_kind="dataset",
        source_path=source_path, dataset_hash=dataset_hash,
    )
    second = store.publish_file(
        job_id="job-2", artifact_kind="dataset",
        source_path=source_path, dataset_hash=dataset_hash,
    )

    assert first["relative_path"] == second["relative_path"]
    assert store.resolve(str(first["relative_path"])).read_bytes() == body


def test_artifact_publish_rejects_traversal_and_wrong_hash(tmp_path) -> None:
    store = ModelArtifactStore(tmp_path)
    with pytest.raises(ArtifactError):
        store.save(
            job_id="../job", artifact_kind="bundle", file_name="model.tgz",
            source=BytesIO(b"x"), expected_sha256=sha256(b"x").hexdigest(),
        )


def test_all_dataset_snapshot_files_are_dataset_scoped_and_immutable(tmp_path) -> None:
    store = ModelArtifactStore(tmp_path / "artifacts")
    dataset_hash = "c" * 64
    for kind, name, body in (
        ("dataset_raw", "dataset_raw.parquet", b"raw"),
        ("dataset_manifest", "dataset_manifest.json", b"{}"),
    ):
        path = tmp_path / name
        path.write_bytes(body)
        saved = store.publish_file(
            job_id="job-1", artifact_kind=kind,
            source_path=path, dataset_hash=dataset_hash,
        )
        assert saved["relative_path"] == f"datasets/{dataset_hash}/{name}"

        path.write_bytes(body + b"changed")
        with pytest.raises(ArtifactError, match="内容不一致"):
            store.publish_file(
                job_id="job-2", artifact_kind=kind,
                source_path=path, dataset_hash=dataset_hash,
            )
    with pytest.raises(ArtifactError, match="SHA256"):
        store.save(
            job_id="job-1", artifact_kind="bundle", file_name="model.tgz",
            source=BytesIO(b"x"), expected_sha256="0" * 64,
        )


def test_retry_atomically_replaces_job_scoped_artifact(tmp_path) -> None:
    store = ModelArtifactStore(tmp_path)
    first = b"first attempt"
    second = b"second attempt"
    store.save(
        job_id="job-1", artifact_kind="bundle", file_name="model.tgz",
        source=BytesIO(first), expected_sha256=sha256(first).hexdigest(),
    )
    replaced = store.save(
        job_id="job-1", artifact_kind="bundle", file_name="model.tgz",
        source=BytesIO(second), expected_sha256=sha256(second).hexdigest(),
    )

    assert store.resolve(str(replaced["relative_path"])).read_bytes() == second


def test_chunked_artifact_upload_assembles_and_checks_each_chunk(tmp_path) -> None:
    store = ModelArtifactStore(tmp_path)
    chunks = [b"first chunk", b"second chunk"]
    for index, body in enumerate(chunks):
        store.save_chunk(
            job_id="job-1", artifact_kind="bundle", file_name="model.tgz",
            upload_id="upload-1", chunk_index=index,
            chunk_sha256=sha256(body).hexdigest(), source=BytesIO(body),
        )
    result = store.assemble_chunks(
        job_id="job-1", artifact_kind="bundle", file_name="model.tgz",
        upload_id="upload-1", total_chunks=2,
        expected_sha256=sha256(b"".join(chunks)).hexdigest(),
    )

    assert store.resolve(str(result["relative_path"])).read_bytes() == b"".join(chunks)


def test_chunked_upload_rejects_missing_and_corrupted_chunks(tmp_path) -> None:
    store = ModelArtifactStore(tmp_path)
    with pytest.raises(ArtifactError, match="分片SHA256"):
        store.save_chunk(
            job_id="job-1", artifact_kind="bundle", file_name="model.tgz",
            upload_id="upload-1", chunk_index=0,
            chunk_sha256="0" * 64, source=BytesIO(b"body"),
        )
    with pytest.raises(ArtifactError, match="不完整"):
        store.assemble_chunks(
            job_id="job-1", artifact_kind="bundle", file_name="model.tgz",
            upload_id="upload-1", total_chunks=2,
            expected_sha256=sha256(b"body").hexdigest(),
        )


def test_chunk_zero_cleans_interrupted_upload_parts(tmp_path) -> None:
    store = ModelArtifactStore(tmp_path)
    stale = b"stale second chunk"
    fresh = b"fresh first chunk"
    store.save_chunk(
        job_id="job-1", artifact_kind="bundle", file_name="model.tgz",
        upload_id="stable-upload", chunk_index=1,
        chunk_sha256=sha256(stale).hexdigest(), source=BytesIO(stale),
    )
    store.save_chunk(
        job_id="job-1", artifact_kind="bundle", file_name="model.tgz",
        upload_id="stable-upload", chunk_index=0,
        chunk_sha256=sha256(fresh).hexdigest(), source=BytesIO(fresh),
    )

    directory = store.root / ".uploads" / "job-1" / "bundle" / "model.tgz" / "stable-upload"
    assert (directory / "00000000.part").read_bytes() == fresh
    assert not (directory / "00000001.part").exists()


def test_model_delete_removes_job_artifacts_but_preserves_shared_dataset(tmp_path) -> None:
    store = ModelArtifactStore(tmp_path / "artifacts")
    dataset_hash = "d" * 64
    body = b"model artifact"
    store.save(
        job_id="job-delete", artifact_kind="bundle", file_name="model.tgz",
        source=BytesIO(body), expected_sha256=sha256(body).hexdigest(),
    )
    dataset_path = tmp_path / "dataset.parquet"
    dataset_path.write_bytes(b"shared dataset")
    store.publish_file(
        job_id="job-delete", artifact_kind="dataset",
        source_path=dataset_path, dataset_hash=dataset_hash,
    )

    removed = store.delete_job_artifacts("job-delete")

    assert removed["job_artifacts"] is True
    assert not (store.root / "job-delete").exists()
    assert (store.root / "datasets" / dataset_hash / "dataset.parquet").exists()
    assert store.delete_dataset_artifacts(dataset_hash) is True
    assert not (store.root / "datasets" / dataset_hash).exists()
