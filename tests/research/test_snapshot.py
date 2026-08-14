from __future__ import annotations

import pandas as pd

from factor_service.research.dataset import PreparedDataset, _frame_fingerprint
from factor_service.research.snapshot import DatasetSnapshotStore
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
