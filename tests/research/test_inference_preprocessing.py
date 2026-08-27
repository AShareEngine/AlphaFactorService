from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from factor_service.research.errors import PermanentJobError
from factor_service.research.inference import DailyInferenceRunner
from factor_service.research.industry_feature import (
    industry_feature_names,
    normalize_industry_feature,
)
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
    def __init__(
        self,
        values: list[float],
        industry_entities: list[str | None] | None = None,
    ) -> None:
        self.values = values
        self.industry_entities = industry_entities
        self.membership_calls = 0
        self.industry_calls = 0

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

    def _industry_membership(
        self, observations: pd.DataFrame, *_args: Any, **_kwargs: Any,
    ) -> pd.DataFrame:
        self.industry_calls += 1
        if self.industry_entities is None:
            raise AssertionError("industry membership must not be requested")
        result = observations[["trade_date", "instrument"]].copy()
        result["industry_entity"] = self.industry_entities
        return result


def _runner(
    values: list[float],
    industry_entities: list[str | None] | None = None,
) -> DailyInferenceRunner:
    runner = DailyInferenceRunner.__new__(DailyInferenceRunner)
    runner.control = _Control()
    runner.dataset_builder = _DatasetBuilder(values, industry_entities)
    return runner


def _training_manifest(
    job: dict[str, Any], *, enabled: bool,
    industry_enabled: bool = False,
) -> dict[str, Any]:
    factor = job["dataset_spec"]["factors"][0]
    feature_name = (
        f"{factor['factor_id']}__v{int(factor['factor_version'])}__"
        f"{str(factor['params_hash'])[:8]}"
    )
    industry_feature = normalize_industry_feature(
        {"enabled": industry_enabled}, default_enabled=False,
    )
    industry_names = industry_feature_names(industry_feature)
    feature_names = [feature_name, *industry_names]
    return {
        "model_kind": "lightgbm",
        "feature_names": feature_names,
        "medians": {name: 0.0 for name in feature_names} | {
            feature_name: 123.0,
        },
        "preprocessing": normalize_feature_preprocessing(
            {"enabled": enabled}, default_enabled=False,
        ),
        "preprocessing_excluded_features": industry_names,
        "industry_feature": industry_feature,
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


def test_daily_inference_appends_training_frozen_industry_columns_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    values = [float(index) for index in range(10)]
    industries = ["801010.SI"] * 9 + ["899999.SI"]
    job = valid_inference_job()
    industry_feature = normalize_industry_feature(
        {"enabled": True}, default_enabled=False,
    )
    job["dataset_spec"]["industry_feature"] = industry_feature
    manifest = _training_manifest(
        job, enabled=True, industry_enabled=True,
    )
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
    runner = _runner(values, industries)

    result = runner.run(job, tmp_path / "industry-infer")

    industry_names = industry_feature_names(industry_feature)
    actual = captured["features"]
    assert actual.columns.tolist() == manifest["feature_names"]
    assert (actual[industry_names].sum(axis=1) == 1.0).all()
    assert actual.loc[0, "industry_sw2021_l1__801010_si"] == 1.0
    assert actual.loc[9, "industry_sw2021_l1__unknown"] == 1.0
    assert set(actual[industry_names].stack().unique()) == {0.0, 1.0}
    assert runner.dataset_builder.industry_calls == 1
    assert result.result["manifest"]["industry_feature"] == industry_feature
    assert result.result["manifest"]["industry_feature_details"][
        "mapped_coverage"
    ] == pytest.approx(0.9)


def test_daily_inference_rejects_industry_contract_or_column_order_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    job = valid_inference_job()
    enabled = normalize_industry_feature(
        {"enabled": True}, default_enabled=False,
    )
    disabled = normalize_industry_feature(
        {"enabled": False}, default_enabled=False,
    )
    manifest = _training_manifest(
        job, enabled=True, industry_enabled=True,
    )
    monkeypatch.setattr(
        "factor_service.research.inference._load_bundle",
        lambda _path: (object(), manifest),
    )
    runner = _runner([1.0] * 10, ["801010.SI"] * 10)
    job["dataset_spec"]["industry_feature"] = disabled

    with pytest.raises(PermanentJobError, match="行业特征口径不一致"):
        runner.run(job, tmp_path / "industry-contract-mismatch")

    assert runner.dataset_builder.membership_calls == 0

    reordered = dict(manifest)
    reordered["feature_names"] = list(manifest["feature_names"])
    reordered["feature_names"][-2:] = reversed(
        reordered["feature_names"][-2:]
    )
    monkeypatch.setattr(
        "factor_service.research.inference._load_bundle",
        lambda _path: (object(), reordered),
    )
    job["dataset_spec"]["industry_feature"] = enabled
    second_runner = _runner([1.0] * 10, ["801010.SI"] * 10)

    with pytest.raises(PermanentJobError, match="特征顺序"):
        second_runner.run(job, tmp_path / "industry-order-mismatch")

    assert second_runner.dataset_builder.membership_calls == 0
