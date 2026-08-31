from __future__ import annotations

import os

import numpy as np
import pandas as pd

from factor_service.research.dataset import PreparedDataset, _frame_fingerprint
from factor_service.research.snapshot import (
    DatasetSnapshotStore,
    prune_stale_dataset_staging,
)
from tests.research.utils import valid_job


def _prepared(job: dict) -> PreparedDataset:
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2024-01-02"), "000001.SZ")],
        names=["datetime", "instrument"],
    )
    columns = pd.MultiIndex.from_tuples([
        ("feature", "mean_amount__v1__aaaaaaaa"),
        ("label", "LABEL0"),
    ])
    raw = pd.DataFrame([[1.5, 0.25]], index=index, columns=columns)
    frame = raw.copy()
    manifest = {
        "dataset_hash": job["dataset_hash"],
        "feature_names": ["mean_amount__v1__aaaaaaaa"],
        "coverage": {"mean_amount": 1.0},
        "medians": {"mean_amount__v1__aaaaaaaa": 1.5},
        "segments": {
            "train": ("2024-01-02", "2024-01-02"),
            "valid": ("2024-01-02", "2024-01-02"),
            "test": ("2024-01-02", "2024-01-02"),
        },
        "content_fingerprint": _frame_fingerprint(frame),
    }
    return PreparedDataset(
        frame=frame,
        raw_frame=raw,
        segments=manifest["segments"],
        feature_names=manifest["feature_names"],
        coverage=manifest["coverage"],
        medians=manifest["medians"],
        manifest=manifest,
    )


def test_snapshot_materializes_in_work_root_and_publishes_canonical_parquet(tmp_path) -> None:
    job = valid_job()
    prepared = _prepared(job)

    class _Builder:
        calls = 0

        def build(self, *_args, **_kwargs):
            self.calls += 1
            return prepared

    builder = _Builder()
    store = DatasetSnapshotStore(tmp_path / "artifacts")
    result = store.get_or_create(job, tmp_path / "work", builder)

    assert result.reused is False
    assert builder.calls == 1
    assert result.dataset_path == (
        tmp_path / "artifacts" / "datasets" / job["dataset_hash"] / "dataset.parquet"
    )
    assert result.dataset_path.is_file()
    assert result.raw_dataset_path.is_file()
    assert result.manifest_path.is_file()
    assert result.prepared.frame.equals(prepared.frame)
    assert not (tmp_path / "work" / "dataset_staging").exists()


def test_snapshot_reuses_canonical_parquet_without_recomputing_factors(tmp_path) -> None:
    job = valid_job()
    store = DatasetSnapshotStore(tmp_path / "artifacts")
    store.get_or_create(
        job, tmp_path / "work-one",
        type("Builder", (), {"build": lambda self, *_args, **_kwargs: _prepared(job)})(),
    )

    class _MustNotBuild:
        def build(self, *_args, **_kwargs):
            raise AssertionError("immutable snapshot should be reused")

    reused = store.get_or_create(job, tmp_path / "work-two", _MustNotBuild())

    assert reused.reused is True
    assert reused.prepared.manifest["dataset_spec_hash"] == job["dataset_hash"]
    assert (
        tmp_path / "artifacts" / "datasets" / job["dataset_hash"] / ".last_used"
    ).is_file()


def test_snapshot_is_regenerated_after_local_cache_expiry(tmp_path) -> None:
    job = valid_job()
    store = DatasetSnapshotStore(tmp_path / "artifacts")

    class _Builder:
        calls = 0

        def build(self, *_args, **_kwargs):
            self.calls += 1
            return _prepared(job)

    builder = _Builder()
    store.get_or_create(job, tmp_path / "work-one", builder)
    store.artifacts.touch_dataset(job["dataset_hash"], used_at=100)
    cleanup = store.artifacts.prune_dataset_cache(
        retention_seconds=100, now=250,
    )
    regenerated = store.get_or_create(job, tmp_path / "work-two", builder)

    assert cleanup["deleted"] == [job["dataset_hash"]]
    assert regenerated.reused is False
    assert builder.calls == 2
    assert regenerated.dataset_path.is_file()


def test_interrupted_dataset_staging_is_removed_after_retention_window(tmp_path) -> None:
    stale = tmp_path / "work" / "jobs" / "job-old" / "dataset_staging"
    fresh = tmp_path / "work" / "jobs" / "job-new" / "dataset_staging"
    for directory, timestamp in ((stale, 100), (fresh, 200)):
        directory.mkdir(parents=True)
        data = directory / "dataset.parquet"
        data.write_bytes(b"temporary duplicate")
        os.utime(data, (timestamp, timestamp))
        os.utime(directory, (timestamp, timestamp))

    result = prune_stale_dataset_staging(
        tmp_path / "work", retention_seconds=100, now=250,
    )

    assert result["scanned"] == 2
    assert result["deleted"] == 1
    assert result["reclaimed_bytes"] == len(b"temporary duplicate")
    assert not stale.exists()
    assert fresh.is_dir()


def test_snapshot_fingerprints_persisted_nan_representation(tmp_path) -> None:
    job = valid_job()
    prepared = _prepared(job)
    prepared.raw_frame.iloc[0, 0] = np.array(
        [0x7FF8000000000001], dtype=np.uint64,
    ).view(np.float64)[0]

    store = DatasetSnapshotStore(tmp_path / "artifacts")
    result = store.get_or_create(
        job, tmp_path / "work",
        type(
            "Builder", (),
            {"build": lambda self, *_args, **_kwargs: prepared},
        )(),
    )

    assert result.prepared.raw_frame.equals(prepared.raw_frame)
    assert result.prepared.manifest["raw_content_fingerprint"] == (
        _frame_fingerprint(result.prepared.raw_frame)
    )
