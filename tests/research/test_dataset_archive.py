from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from factor_service.model_artifacts import ArtifactError, ModelArtifactStore
from factor_service.model_object_store import ModelObjectStore, ModelObjectStoreIntegrityError
from factor_service.research.dataset_archive import DATASET_FILES, DatasetArchive
from factor_service.research.snapshot import DatasetSnapshotStore
from tests.research.test_snapshot import _prepared
from tests.research.utils import valid_job
from tests.test_model_object_store import _MemoryMinio, _config


class MemoryRegistry:
    def __init__(self):
        self.rows = {}
        self.failure = False

    def get(self, key):
        return deepcopy(self.rows.get(key))

    def register(self, key, *, spec, manifest, files):
        if self.failure:
            raise RuntimeError("registry unavailable")
        row = dict(dataset_hash=key, spec_json=spec, manifest_json=manifest, files_json=files)
        self.rows.setdefault(key, deepcopy(row))
        return deepcopy(self.rows[key])


@pytest.fixture
def setup(tmp_path):
    job = valid_job()
    client = _MemoryMinio()
    registry = MemoryRegistry()
    archive = DatasetArchive(
        ModelArtifactStore(tmp_path / "artifacts"),
        ModelObjectStore(_config(), client=client), registry,
        active_hashes=lambda: set(),
    )
    store = DatasetSnapshotStore(tmp_path / "artifacts", archive=archive)

    class Builder:
        calls = 0

        def build(self, *args, **kwargs):
            self.calls += 1
            return _prepared(job)

    return job, archive, store, Builder(), client, registry


def build(setup, tmp_path):
    job, archive, store, builder, _, _ = setup
    result = store.get_or_create(job, tmp_path / "work", builder)
    return result


def test_snapshot_archive_evict_restore_never_rebuilds(setup, tmp_path):
    job, archive, store, builder, client, registry = setup
    first = build(setup, tmp_path)
    before = {name: (first.dataset_path.parent / name).read_bytes() for name in DATASET_FILES}
    assert set(registry.get(job['dataset_hash'])['files_json']) == set(DATASET_FILES)
    assert len(client.uploads) == 3
    assert archive.evict(job['dataset_hash']) > 0
    assert not first.dataset_path.parent.exists()
    second = store.get_or_create(job, tmp_path / "work2", builder)
    assert second.reused
    assert builder.calls == 1
    assert len(client.uploads) == 3
    assert {n: (second.dataset_path.parent / n).read_bytes() for n in DATASET_FILES} == before
    pd.testing.assert_frame_equal(first.prepared.frame, second.prepared.frame)


def test_same_dataset_shared_by_jobs_not_model_versions(setup, tmp_path):
    job, archive, store, builder, client, _ = setup
    build(setup, tmp_path)
    archive.evict(job['dataset_hash'])
    changed = {**job, 'job_id': 'another-job', 'model_id': 'another-model'}
    store.get_or_create(changed, tmp_path / "other", builder)
    assert builder.calls == 1
    assert len(client.uploads) == 3
    assert all(f"/datasets/{job['dataset_hash']}/" in x['object_key'] for x in client.uploads)


def test_restore_does_not_initialize_source_database_builder(setup, tmp_path):
    job, archive, store, _, _, _ = setup
    build(setup, tmp_path)
    archive.evict(job['dataset_hash'])

    def offline_source():
        pytest.fail('MinIO restore must not initialize or connect a dataset builder')

    snapshot = store.get_or_create(
        job, tmp_path / 'restored', None, builder_factory=offline_source,
    )
    assert snapshot.reused


def test_first_build_initializes_source_builder_only_once(setup, tmp_path):
    job, archive, store, builder, _, _ = setup
    calls = []

    def factory():
        calls.append(True)
        return builder

    store.get_or_create(job, tmp_path / 'first', None, builder_factory=factory)
    archive.evict(job['dataset_hash'])
    store.get_or_create(job, tmp_path / 'second', None, builder_factory=factory)
    assert calls == [True]


def test_database_failure_preserves_only_local_copy(setup, tmp_path):
    job, archive, _, builder, _, registry = setup
    registry.failure = True
    with pytest.raises(RuntimeError, match='registry unavailable'):
        build(setup, tmp_path)
    directory = archive.directory(job['dataset_hash'])
    assert all((directory / n).is_file() for n in DATASET_FILES)
    assert archive.cleanup()['deleted'] == []
    registry.failure = False
    build(setup, tmp_path)
    assert builder.calls == 1
    assert archive.evict(job['dataset_hash']) > 0


def test_partial_upload_never_registers_or_evicts(setup, tmp_path, monkeypatch):
    job, archive, _, _, client, registry = setup
    original = client.fput_object

    def fail_second(*args, **kwargs):
        if client.uploads:
            raise OSError('network failure')
        return original(*args, **kwargs)

    monkeypatch.setattr(client, 'fput_object', fail_second)
    with pytest.raises(Exception, match='连接失败'):
        build(setup, tmp_path)
    assert registry.get(job['dataset_hash']) is None
    assert archive.evict(job['dataset_hash']) == 0
    assert all((archive.directory(job['dataset_hash']) / n).is_file() for n in DATASET_FILES)


def test_upload_readback_checks_bytes_not_only_metadata(setup, tmp_path, monkeypatch):
    job, archive, _, _, client, registry = setup
    original = client.fput_object

    def corrupt(*args, **kwargs):
        result = original(*args, **kwargs)
        key = (args[0], args[1])
        client.bodies[key] = b'x' * len(client.bodies[key])
        return result

    monkeypatch.setattr(client, 'fput_object', corrupt)
    with pytest.raises(ModelObjectStoreIntegrityError, match='内容SHA256'):
        build(setup, tmp_path)
    assert registry.get(job['dataset_hash']) is None
    assert archive.directory(job['dataset_hash']).exists()


@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'network'])
def test_restore_errors_do_not_silently_recompute(setup, tmp_path, monkeypatch, failure):
    job, archive, store, builder, client, _ = setup
    build(setup, tmp_path)
    archive.evict(job['dataset_hash'])
    key = next(iter(client.objects))
    if failure == 'missing':
        del client.objects[key]
    elif failure == 'corrupt':
        client.bodies[key] = b'x' * len(client.bodies[key])
    else:
        monkeypatch.setattr(client, 'get_object', lambda *a, **kw: (_ for _ in ()).throw(OSError('offline')))
    with pytest.raises(Exception):
        store.get_or_create(job, tmp_path / 'again', builder)
    assert builder.calls == 1
    assert not (archive.directory(job['dataset_hash']) / 'dataset_manifest.json').exists()


def test_existing_local_content_cannot_replace_archive(setup, tmp_path):
    job, archive, store, builder, client, registry = setup
    result = build(setup, tmp_path)
    record = registry.get(job['dataset_hash'])
    result.dataset_path.write_bytes(b'changed')
    with pytest.raises(ArtifactError, match='不同'):
        store.get_or_create(job, tmp_path / 'again', builder)
    with pytest.raises(ArtifactError, match='不同'):
        archive.evict(job['dataset_hash'])
    assert registry.get(job['dataset_hash']) == record
    assert len(client.uploads) == 3
    assert result.dataset_path.exists()


def test_active_job_and_whole_reader_lifetime_protect_files(setup, tmp_path):
    job, archive, _, _, _, _ = setup
    result = build(setup, tmp_path)
    key = job['dataset_hash']
    assert archive.evict(key, protected_hashes={key}) == 0
    with archive.artifacts.dataset_usage(key):
        assert archive.evict(key) == 0
        assert result.dataset_path.exists()
    assert archive.evict(key) > 0


def test_last_reader_evicts_not_first(setup, tmp_path):
    job, archive, _, _, _, _ = setup
    build(setup, tmp_path)
    key = job['dataset_hash']
    with archive.use(key) as directory:
        with archive.use(key):
            assert (directory / 'dataset.parquet').is_file()
        assert directory.is_dir()
    assert not directory.exists()


def test_streaming_failure_releases_dataset_pin(setup, tmp_path, monkeypatch):
    import asyncio
    from contextlib import ExitStack
    from fastapi.responses import FileResponse
    from factor_service.api.model_research import _PinnedDatasetFileResponse

    job, archive, _, _, _, _ = setup
    build(setup, tmp_path)
    key = job['dataset_hash']
    usage = ExitStack()
    directory = usage.enter_context(archive.use(key))

    async def fail_stream(*args):
        raise OSError("client disconnected")

    monkeypatch.setattr(FileResponse, '__call__', fail_stream)
    response = _PinnedDatasetFileResponse(directory / 'dataset.parquet', usage=usage)
    with pytest.raises(OSError, match='client disconnected'):
        asyncio.run(response({}, None, None))
    assert not directory.exists()


def test_diagnostic_failure_still_releases_temporary_copy(setup, tmp_path):
    job, archive, _, _, _, _ = setup
    build(setup, tmp_path)
    with pytest.raises(RuntimeError):
        with archive.use(job['dataset_hash']) as directory:
            raise RuntimeError('diagnostic failed')
    assert not directory.exists()


def test_unknown_file_prevents_cleanup(setup, tmp_path):
    job, archive, _, _, _, _ = setup
    result = build(setup, tmp_path)
    (result.dataset_path.parent / 'user-file').write_text('keep')
    with pytest.raises(ArtifactError, match='未知文件'):
        archive.evict(job['dataset_hash'])
    assert result.dataset_path.exists()


def test_missing_remote_file_preserves_every_local_file(setup, tmp_path):
    job, archive, _, _, client, _ = setup
    result = build(setup, tmp_path)
    del client.objects[list(client.objects)[-1]]
    with pytest.raises(Exception):
        archive.evict(job['dataset_hash'])
    assert all((result.dataset_path.parent / name).exists() for name in DATASET_FILES)


def test_partial_download_resumes_with_manifest_last(setup, tmp_path, monkeypatch):
    job, archive, store, builder, client, _ = setup
    build(setup, tmp_path)
    archive.evict(job['dataset_hash'])
    original = client.get_object
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 2:
            raise OSError('offline')
        return original(*args, **kwargs)

    monkeypatch.setattr(client, 'get_object', fail_second)
    with pytest.raises(Exception):
        store.get_or_create(job, tmp_path / 'again', builder)
    directory = archive.directory(job['dataset_hash'])
    assert (directory / 'dataset.parquet').exists()
    assert not (directory / 'dataset_manifest.json').exists()
    monkeypatch.setattr(client, 'get_object', original)
    assert store.get_or_create(job, tmp_path / 'again', builder).reused
    assert builder.calls == 1


def test_path_traversal_and_symlink_are_rejected(setup, tmp_path):
    job, archive, *_ = setup
    with pytest.raises(ArtifactError):
        archive.directory('../escape')
    root = archive.artifacts.root / 'datasets'
    root.mkdir()
    (root / job['dataset_hash']).symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ArtifactError, match='符号链接'):
        archive.directory(job['dataset_hash'])


def test_worker_records_dataset_object_identities_before_cleanup(setup, tmp_path):
    from factor_service.research.trainer import TrainingResult
    from factor_service.research.worker import ResearchWorker
    from tests.research.test_worker_reliability import _Api, _settings

    job, archive, _, _, _, registry = setup
    snapshot = build(setup, tmp_path)
    predictions = tmp_path / 'predictions.parquet'
    predictions.write_bytes(b'empty test predictions')

    class Trainer:
        def train(self, *args, **kwargs):
            return TrainingResult(
                result={'predictions': {'row_count': 0}},
                artifacts=[(kind, snapshot.dataset_path.parent / name) for name, kind in DATASET_FILES.items()],
                predictions_path=predictions,
            )

        def publish_predictions(self, *args, **kwargs):
            return 0

    worker = ResearchWorker(_settings(tmp_path))
    api = _Api()
    worker.control = api
    worker.trainer = Trainer()
    worker.dataset_archive = archive
    worker._run_job(job)
    assert not api.failed
    assert api.completed == [job['job_id']]
    assert len(api.artifacts) == 3
    for artifact in api.artifacts:
        stored = registry.get(job['dataset_hash'])['files_json'][artifact['file_name']]
        assert artifact['object_store_uri'] == stored['object_uri']
        assert artifact['object_store_sha256'] == stored['sha256']
    assert not snapshot.dataset_path.parent.exists()


def test_dataset_diagnostic_hydrates_and_evicts(setup, tmp_path, monkeypatch):
    import factor_service.model_diagnostics as diagnostics
    import factor_service.research.dataset_archive as module

    job, archive, _, _, _, _ = setup
    build(setup, tmp_path)
    archive.evict(job['dataset_hash'])
    monkeypatch.setattr(module, 'dataset_files', lambda h, root: archive.use(h))
    seen = []

    @diagnostics._with_dataset_files
    def read(dataset_hash, artifact_root):
        directory = Path(artifact_root) / 'datasets' / dataset_hash
        frame = pd.read_parquet(directory / 'dataset.parquet')
        seen.append(len(frame))
        return frame

    assert len(read(job['dataset_hash'], archive.artifacts.root)) == 1
    assert seen == [1]
    assert not archive.directory(job['dataset_hash']).exists()
