from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from factor_service.research.errors import PermanentJobError
from factor_service.research.inference import DailyInferenceRunner
from factor_service.research.preprocessing import normalize_feature_preprocessing
from tests.research.utils import valid_inference_job


class _Control:
    def download_artifact(
        self, artifact_id: str, destination: Path, expected_sha256: str,
    ) -> Path:
        assert artifact_id == "artifact_model_bundle"
        assert expected_sha256 == "b" * 64
        return destination


class _DatasetBuilder:
    def __init__(self, values: list[float]) -> None:
        self.values = values
        self.membership_calls = 0

    def _membership(self, *_args: Any, **_kwargs: Any) -> pd.DataFrame:
        self.membership_calls += 1
        return pd.DataFrame({
            "trade_date": pd.to_datetime(["2024-12-31"] * len(self.values)),
            "instrument": [f"{index:06d}.SZ" for index in range(len(self.values))],
        })

    def _factor_values(self, *_args: Any, **_kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame({
            "trade_date": pd.to_datetime(["2024-12-31"] * len(self.values)),
            "instrument": [f"{index:06d}.SZ" for index in range(len(self.values))],
            "value": self.values,
        })


def _runner(values: list[float]) -> DailyInferenceRunner:
    runner = DailyInferenceRunner.__new__(DailyInferenceRunner)
    runner.control = _Control()
    runner.dataset_builder = _DatasetBuilder(values)
    return runner


def _training_manifest(job: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    factor = job["dataset_spec"]["factors"][0]
    feature_name = (
        f"{factor['factor_id']}__v{int(factor['factor_version'])}__"
        f"{str(factor['params_hash'])[:8]}"
    )
    return {
        "model_kind": "lightgbm",
        "feature_names": [feature_name],
        "medians": {feature_name: 123.0},
        "preprocessing": normalize_feature_preprocessing(
            {"enabled": enabled}, default_enabled=False,
        ),
        "preprocessing_excluded_features": [],
    }


def test_daily_inference_applies_preprocessing_frozen_in_training_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    job = valid_inference_job()
    # The inference request intentionally omits the new field. The immutable
    # training manifest remains the source of truth for old scheduled jobs.
    manifest = _training_manifest(job, enabled=True)
    monkeypatch.setattr(
        "factor_service.research.inference._load_bundle",
        lambda _path: (object(), manifest),
    )
    captured: dict[str, pd.DataFrame] = {}

    def predict(_model: Any, _kind: str, features: pd.DataFrame) -> np.ndarray:
        captured["features"] = features.copy()
        return np.arange(len(features), dtype=float)

    monkeypatch.setattr(
        "factor_service.research.inference.predict_feature_frame", predict,
    )
    raw = [np.nan, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 100.0]

    result = _runner(raw).run(job, tmp_path / "infer")

    feature_name = manifest["feature_names"][0]
    actual = captured["features"][feature_name].to_numpy(dtype=float)
    filled = np.asarray([5.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 100.0])
    clipped = np.clip(filled, np.quantile(filled, 0.01), np.quantile(filled, 0.99))
    expected = (clipped - clipped.mean()) / clipped.std(ddof=0)

    np.testing.assert_allclose(actual, expected)
    assert np.isfinite(actual).all()
    assert actual.mean() == pytest.approx(0.0, abs=1e-12)
    assert actual.std(ddof=0) == pytest.approx(1.0)
    assert actual[0] == pytest.approx(actual[5])
    assert result.result["manifest"]["preprocessing"] == manifest["preprocessing"]
    assert (
        "training-identical same-date cross-sectional median, 1/99 winsorization and z-score"
        in result.result["manifest"]["future_function_guards"]
    )


def test_daily_inference_rejects_preprocessing_different_from_training_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    job = valid_inference_job()
    job["dataset_spec"]["preprocessing"] = normalize_feature_preprocessing(
        {"enabled": False}, default_enabled=False,
    )
    manifest = _training_manifest(job, enabled=True)
    monkeypatch.setattr(
        "factor_service.research.inference._load_bundle",
        lambda _path: (object(), manifest),
    )
    runner = _runner([1.0, 2.0])

    with pytest.raises(PermanentJobError, match="特征预处理口径不一致"):
        runner.run(job, tmp_path / "mismatch")

    assert runner.dataset_builder.membership_calls == 0
