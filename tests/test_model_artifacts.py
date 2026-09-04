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


def test_dataset_cache_prunes_only_expired_unprotected_snapshots(tmp_path) -> None:
    store = ModelArtifactStore(tmp_path / "artifacts")
    expired_hash = "1" * 64
    protected_hash = "2" * 64
    fresh_hash = "3" * 64
    source = tmp_path / "dataset.parquet"
    source.write_bytes(b"temporary training dataset")
    for dataset_hash in (expired_hash, protected_hash, fresh_hash):
        store.publish_file(
            job_id="job-cache", artifact_kind="dataset",
            source_path=source, dataset_hash=dataset_hash,
        )
    store.touch_dataset(expired_hash, used_at=100)
    store.touch_dataset(protected_hash, used_at=100)
    store.touch_dataset(fresh_hash, used_at=200)

    result = store.prune_dataset_cache(
        retention_seconds=100,
        protected_hashes={protected_hash},
        now=250,
    )

    assert result["deleted"] == [expired_hash]
    assert result["protected"] == 1
    assert result["reclaimed_bytes"] >= len(source.read_bytes())
    assert not (store.root / "datasets" / expired_hash).exists()
    assert (store.root / "datasets" / protected_hash).is_dir()
    assert (store.root / "datasets" / fresh_hash).is_dir()


def test_dataset_cache_skips_snapshot_held_by_active_file_lock(tmp_path) -> None:
    store = ModelArtifactStore(tmp_path / "artifacts")
    dataset_hash = "4" * 64
    source = tmp_path / "dataset.parquet"
    source.write_bytes(b"in-use dataset")
    store.publish_file(
        job_id="job-active", artifact_kind="dataset",
        source_path=source, dataset_hash=dataset_hash,
    )
    store.touch_dataset(dataset_hash, used_at=100)

    with store.dataset_lock(dataset_hash):
        result = store.prune_dataset_cache(retention_seconds=100, now=250)

    assert result["deleted"] == []
    assert result["locked"] == 1
    assert (store.root / "datasets" / dataset_hash).is_dir()


def test_legacy_cleanup_and_model_deletion_preserve_pinned_dataset(tmp_path) -> None:
    store = ModelArtifactStore(tmp_path / "artifacts")
    dataset_hash = "e" * 64
    directory = store.root / "datasets" / dataset_hash
    directory.mkdir(parents=True)
    (directory / "dataset.parquet").write_bytes(b"pinned data")
    store.touch_dataset(dataset_hash, used_at=100)
    with store.dataset_usage(dataset_hash):
        assert store.delete_dataset_artifacts(dataset_hash) is False
        result = store.prune_dataset_cache(retention_seconds=100, now=250)
        assert result["deleted"] == []
        assert result["locked"] == 1
        assert directory.exists()
    assert store.delete_dataset_artifacts(dataset_hash) is True


def test_resolving_dataset_artifact_refreshes_last_used_marker(tmp_path) -> None:
    store = ModelArtifactStore(tmp_path / "artifacts")
    dataset_hash = "7" * 64
    source = tmp_path / "dataset.parquet"
    source.write_bytes(b"diagnostic dataset")
    published = store.publish_file(
        job_id="job-diagnostic", artifact_kind="dataset",
        source_path=source, dataset_hash=dataset_hash,
    )
    marker = store.touch_dataset(dataset_hash, used_at=100)

    resolved = store.resolve(str(published["relative_path"]))

    assert resolved.read_bytes() == b"diagnostic dataset"
    assert marker.stat().st_mtime > 100
