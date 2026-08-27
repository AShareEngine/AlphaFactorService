from __future__ import annotations

import json
from pathlib import Path
import pickle
import sqlite3
import tarfile
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from factor_service.research.trainer import QlibStackingModel

from factor_service.model_diagnostics import (
    _ks_statistic,
    _population_stability_index,
    artifact_model_feature_importance,
    artifact_model_permutation_importance,
    artifact_model_shap_summary,
    artifact_model_training_diagnostics,
    architecture_walk_forward_attribution,
    dataset_feature_drift,
    dataset_feature_redundancy,
    dataset_factor_validation_audit,
    dataset_walk_forward_attribution,
    isolated_artifact_model_permutation_importance,
)


class _QlibStyleImportanceModel:
    @staticmethod
    def get_feature_importance():
        return pd.Series({"Column_0": 9, "Column_1": 4})


class _LinearDatasetModel:
    @staticmethod
    def predict(dataset, segment="test"):
        from qlib.data.dataset import DataHandlerLP

        features = dataset.prepare(
            segment, col_set="feature", data_key=DataHandlerLP.DK_I,
        )
        return pd.Series(features.iloc[:, 0].to_numpy(), index=features.index)


class _AverageMetaModel:
    @staticmethod
    def predict(values):
        return np.asarray(values, dtype=float).mean(axis=1)


class _FakeLightGBMBooster:
    best_iteration = 7

    @staticmethod
    def predict(values, *, num_iteration=None, pred_contrib=False):
        matrix = np.asarray(values, dtype=float)
        if pred_contrib:
            return np.column_stack((
                matrix[:, 0] * 2.0,
                matrix[:, 1] * -1.0,
                np.full(len(matrix), 0.5),
            ))
        return matrix[:, 0] * 2.0 - matrix[:, 1] + 0.5


class _FakeTreeModel:
    model = _FakeLightGBMBooster()


def test_distribution_metrics_distinguish_stable_and_shifted_samples() -> None:
    reference = pd.Series(np.linspace(-1, 1, 100))

    assert _population_stability_index(reference, reference) == pytest.approx(0.0)
    assert _ks_statistic(reference, reference) == pytest.approx(0.0)
    assert _population_stability_index(reference, reference + 3) > 0.25
    assert _ks_statistic(reference, reference + 3) == pytest.approx(1.0)


def test_feature_drift_reads_immutable_raw_snapshot(tmp_path: Path) -> None:
    dataset_hash = "a" * 64
    dataset_dir = tmp_path / "datasets" / dataset_hash
    dataset_dir.mkdir(parents=True)
    dates = pd.date_range("2024-01-01", periods=6)
    index = pd.MultiIndex.from_product(
        [dates, ["A", "B"]], names=["datetime", "instrument"],
    )
    stable = np.tile([-1.0, 1.0], 6)
    shifted = np.concatenate((np.zeros(8), np.full(4, 3.0)))
    raw = pd.DataFrame(
        np.column_stack((stable, shifted, np.zeros(len(index)))),
        index=index,
        columns=pd.MultiIndex.from_tuples([
            ("feature", "stable_factor"),
            ("feature", "shifted_factor"),
            ("label", "LABEL0"),
        ]),
    )
    raw.to_parquet(dataset_dir / "dataset_raw.parquet")
    (dataset_dir / "dataset_manifest.json").write_text(json.dumps({
        "dataset_spec_hash": dataset_hash,
        "feature_names": ["stable_factor", "shifted_factor"],
        "segments": {
            "train": ["2024-01-01", "2024-01-04"],
            "valid": ["2024-01-05", "2024-01-05"],
            "test": ["2024-01-05", "2024-01-06"],
        },
    }), encoding="utf-8")

    result = dataset_feature_drift(dataset_hash, tmp_path)
    by_factor = {item["factor"]: item for item in result["features"]}

    assert result["train_rows"] == 8
    assert result["test_rows"] == 4
    assert result["counts"] == {"stable": 1, "medium": 0, "severe": 1}
    assert by_factor["stable_factor"]["status"] == "stable"
    assert by_factor["shifted_factor"]["status"] == "severe"
    assert by_factor["shifted_factor"]["ks"] == pytest.approx(1.0)


def test_feature_drift_rejects_unsafe_dataset_hash(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dataset_hash"):
        dataset_feature_drift("../outside", tmp_path)


def test_walk_forward_attribution_finds_factor_reversal(
    tmp_path: Path,
) -> None:
    dataset_hash = "c" * 64
    dataset_dir = tmp_path / "datasets" / dataset_hash
    dataset_dir.mkdir(parents=True)
    dates = pd.date_range("2024-01-01", periods=8)
    instruments = [f"S{index:02d}" for index in range(40)]
    index = pd.MultiIndex.from_product(
        [dates, instruments], names=["datetime", "instrument"],
    )
    feature = np.tile(np.linspace(-1, 1, len(instruments)), len(dates))
    label = np.concatenate([
        np.linspace(-1, 1, len(instruments)) if day < 4
        else np.linspace(1, -1, len(instruments))
        for day in range(len(dates))
    ])
    raw = pd.DataFrame(
        np.column_stack((feature, label)),
        index=index,
        columns=pd.MultiIndex.from_tuples([
            ("feature", "signal"), ("label", "LABEL0"),
        ]),
    )
    raw.to_parquet(dataset_dir / "dataset_raw.parquet")
    (dataset_dir / "dataset_manifest.json").write_text(json.dumps({
        "dataset_spec_hash": dataset_hash,
        "feature_names": ["signal"],
    }), encoding="utf-8")

    result = dataset_walk_forward_attribution(dataset_hash, tmp_path, {
        "enabled": True,
        "windows": [{
            "window": 1,
            "segments": {
                "train": ["2024-01-01", "2024-01-04"],
                "test": ["2024-01-05", "2024-01-08"],
            },
            "metrics": {"rank_ic": -0.2, "ic_ir": -0.5},
        }],
    })

    factor = result["windows"][0]["features"][0]
    assert result["primary_cause"] == "factor_sign_reversal"
    assert result["weak_window"]["window"] == 1
    assert factor["train_rank_ic"] == pytest.approx(1.0)
    assert factor["test_rank_ic"] == pytest.approx(-1.0)
    assert factor["status"] == "reversed"
    assert factor["sign_flip"] is True


def test_architecture_walk_forward_attribution_identifies_common_weak_window() -> None:
    excess = {
        "stock_only": [0.10, -0.20],
        "industry_stock": [0.15, -0.16],
        "full": [0.17, -0.19],
    }
    backtests = []
    for profile, values in excess.items():
        backtests.append({
            "status": "success",
            "configuration": {"ablation_profile": profile},
            "payload": {"walk_forward": {
                "status": "mixed",
                "windows": [{
                    "window": index + 1,
                    "test_start": f"202{index + 3}-01-01",
                    "test_end": f"202{index + 3}-12-31",
                    "complete": True,
                    "annual_return": value + (0.08 if index == 0 else 0.30),
                    "excess_annual_return": value,
                } for index, value in enumerate(values)],
            }},
        })

    result = architecture_walk_forward_attribution(backtests)

    assert result["eligible"] is True
    assert result["weak_window"]["window"] == 2
    assert result["weak_window"]["all_profiles_negative"] is True
    assert result["weak_window"]["market_regime"] == "strong_bull"
    assert result["common_failure_window_count"] == 1
    assert result["gate_contributions"]["industry_vs_stock_mean"] == pytest.approx(0.045)


def test_feature_redundancy_uses_train_only_daily_cross_sections(
    tmp_path: Path,
) -> None:
    dataset_hash = "d" * 64
    dataset_dir = tmp_path / "datasets" / dataset_hash
    dataset_dir.mkdir(parents=True)
    dates = pd.date_range("2024-01-01", periods=8)
    instruments = [f"S{index:02d}" for index in range(40)]
    index = pd.MultiIndex.from_product(
        [dates, instruments], names=["datetime", "instrument"],
    )
    base = np.tile(np.linspace(-1, 1, len(instruments)), len(dates))
    rng = np.random.default_rng(42)
    related = base + rng.normal(0, 0.03, len(base))
    independent = np.concatenate([
        rng.permutation(np.linspace(-1, 1, len(instruments))) for _ in dates
    ])
    raw = pd.DataFrame(
        np.column_stack((base, related, independent, base)),
        index=index,
        columns=pd.MultiIndex.from_tuples([
            ("feature", "leader"), ("feature", "related"),
            ("feature", "independent"), ("label", "LABEL0"),
        ]),
    )
    raw.to_parquet(dataset_dir / "dataset_raw.parquet")
    (dataset_dir / "dataset_manifest.json").write_text(json.dumps({
        "dataset_spec_hash": dataset_hash,
        "feature_names": ["leader", "related", "independent"],
        "segments": {
            "train": ["2024-01-01", "2024-01-05"],
            "valid": ["2024-01-06", "2024-01-06"],
            "test": ["2024-01-07", "2024-01-08"],
        },
    }), encoding="utf-8")

    result = dataset_feature_redundancy(dataset_hash, tmp_path, threshold=0.85)

    assert result["train_days"] == 5
    assert result["high_correlation_pair_count"] == 1
    assert result["redundancy_group_count"] == 1
    assert result["groups"][0]["features"] == ["leader", "related"]
    assert result["groups"][0]["recommended_keep"] == "leader"
    assert result["groups"][0]["review_candidates"] == ["related"]
    assert result["method"]["selection_scope"] == "train_only"
    assert result["matrix"][0][1] > 0.95


def test_feature_redundancy_validates_threshold(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="相关性阈值"):
        dataset_feature_redundancy("e" * 64, tmp_path, threshold=0.2)


def test_factor_validation_audit_never_reads_test_segment(tmp_path: Path) -> None:
    dataset_hash = "a" * 64
    dataset_dir = tmp_path / "datasets" / dataset_hash
    dataset_dir.mkdir(parents=True)
    dates = pd.date_range("2024-01-01", periods=9)
    instruments = [f"S{index:02d}" for index in range(40)]
    index = pd.MultiIndex.from_product(
        [dates, instruments], names=["datetime", "instrument"],
    )
    cross_section = np.tile(np.linspace(-1, 1, len(instruments)), len(dates))
    stable = cross_section.copy()
    reversed_feature = cross_section.copy()
    labels = cross_section.copy()
    valid_mask = index.get_level_values("datetime").isin(dates[4:7])
    reversed_feature[valid_mask] *= -1
    test_mask = index.get_level_values("datetime").isin(dates[7:])
    stable[test_mask] *= -1000
    raw = pd.DataFrame(
        np.column_stack((stable, reversed_feature, labels)),
        index=index,
        columns=pd.MultiIndex.from_tuples([
            ("feature", "stable__v1__one"),
            ("feature", "reverse__v1__two"),
            ("label", "LABEL0"),
        ]),
    )
    raw.to_parquet(dataset_dir / "dataset_raw.parquet")
    (dataset_dir / "dataset_manifest.json").write_text(json.dumps({
        "dataset_spec_hash": dataset_hash,
        "feature_names": ["stable__v1__one", "reverse__v1__two"],
        "coverage": {"stable": 1.0, "reverse": 0.95},
        "segments": {
            "train": ["2024-01-01", "2024-01-04"],
            "valid": ["2024-01-05", "2024-01-07"],
            "test": ["2024-01-08", "2024-01-09"],
        },
    }), encoding="utf-8")

    result = dataset_factor_validation_audit(dataset_hash, tmp_path)

    by_id = {item["factor_id"]: item for item in result["factors"]}
    assert result["method"]["test_segment_read"] is False
    assert result["method"]["selection_scope"] == "train_and_validation_only"
    assert by_id["stable"]["status"] == "stable"
    assert by_id["stable"]["valid"]["rank_ic"] == pytest.approx(1.0)
    assert by_id["reverse"]["status"] == "reversed"
    assert by_id["reverse"]["coverage"] == pytest.approx(0.95)


def test_model_bundle_importance_repairs_qlib_column_names(tmp_path: Path) -> None:
    model_path = tmp_path / "model.pkl"
    model_path.write_bytes(pickle.dumps(_QlibStyleImportanceModel()))
    bundle_path = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as archive:
        archive.add(model_path, arcname="model.pkl")

    result = artifact_model_feature_importance(bundle_path, ["amount", "momentum"])

    assert result == [
        {"factor": "amount", "importance": 9.0, "rank": 1},
        {"factor": "momentum", "importance": 4.0, "rank": 2},
    ]


def test_tree_shap_summary_uses_frozen_validation_sample(tmp_path: Path) -> None:
    dates = pd.date_range("2024-01-01", periods=4)
    instruments = [f"S{index:02d}" for index in range(10)]
    index = pd.MultiIndex.from_product(
        [dates, instruments], names=["datetime", "instrument"],
    )
    first = np.tile(np.linspace(-1, 1, len(instruments)), len(dates))
    second = np.tile(np.linspace(0, 0.5, len(instruments)), len(dates))
    frame = pd.DataFrame(
        np.column_stack((first, second, first)),
        index=index,
        columns=pd.MultiIndex.from_tuples([
            ("feature", "signal"), ("feature", "noise"),
            ("label", "LABEL0"),
        ]),
    )
    dataset_path = tmp_path / "dataset.parquet"
    frame.to_parquet(dataset_path)
    model_path = tmp_path / "model.pkl"
    model_path.write_bytes(pickle.dumps(_FakeTreeModel()))
    bundle_path = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as archive:
        archive.add(model_path, arcname="model.pkl")

    result = artifact_model_shap_summary(
        bundle_path,
        dataset_path,
        model_kind="lightgbm",
        segments={
            "train": ["2024-01-01", "2024-01-01"],
            "valid": ["2024-01-02", "2024-01-03"],
            "test": ["2024-01-04", "2024-01-04"],
        },
        feature_names=["signal", "noise"],
        split="valid",
        sample_rows=12,
    )

    assert result["split"] == "valid"
    assert result["rows_available"] == 20
    assert result["rows_used"] == 12
    assert result["features"][0]["factor"] == "signal"
    assert sum(item["contribution_share"] for item in result["features"]) == pytest.approx(1.0)
    assert result["local_accuracy_max_abs_error"] == pytest.approx(0.0)
    assert result["method"]["labels_used"] is False


def test_training_diagnostics_recovers_historical_mlflow_metrics(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE metrics (key TEXT, value REAL, timestamp INTEGER, "
        "run_uuid TEXT, step INTEGER, is_nan INTEGER)",
    )
    connection.executemany(
        "INSERT INTO metrics VALUES (?, ?, ?, 'run', ?, 0)",
        [
            ("final.l2.train", 0.50, 1, 0),
            ("final.l2.valid", 0.52, 1, 0),
            ("final.l2.train", 0.30, 2, 1),
            ("final.l2.valid", 0.34, 2, 1),
            ("final.l2.train", 0.20, 3, 2),
            ("final.l2.valid", 0.36, 3, 2),
        ],
    )
    database_path = tmp_path / "mlflow.db"
    database_path.write_bytes(connection.serialize())
    connection.close()
    bundle_path = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as archive:
        archive.add(database_path, arcname="mlflow.db")

    result = artifact_model_training_diagnostics(
        bundle_path,
        model_kind="lightgbm",
        model_params={"num_boost_round": 20, "early_stopping_rounds": 2},
    )

    assert result["available"] is True
    assert result["metric"] == "l2"
    assert result["best_iteration"] == 2
    assert result["trained_iterations"] == 3
    assert result["history_point_count"] == 3


def test_permutation_importance_uses_independent_test_cross_sections(
    tmp_path: Path,
) -> None:
    dates = pd.date_range("2024-01-01", periods=10)
    instruments = [f"S{index:02d}" for index in range(10)]
    index = pd.MultiIndex.from_product(
        [dates, instruments], names=["datetime", "instrument"],
    )
    signal = np.tile(np.linspace(-1, 1, len(instruments)), len(dates))
    noise = np.tile(np.arange(len(instruments)) % 2, len(dates)).astype(float)
    frame = pd.DataFrame(
        np.column_stack((signal, noise, signal)),
        index=index,
        columns=pd.MultiIndex.from_tuples([
            ("feature", "signal"), ("feature", "noise"),
            ("label", "LABEL0"),
        ]),
    )
    dataset_path = tmp_path / "dataset.parquet"
    frame.to_parquet(dataset_path)
    model_path = tmp_path / "model.pkl"
    model_path.write_bytes(pickle.dumps(_LinearDatasetModel()))
    bundle_path = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as archive:
        archive.add(model_path, arcname="model.pkl")

    result = artifact_model_permutation_importance(
        bundle_path,
        dataset_path,
        model_kind="lightgbm",
        segments={
            "train": ["2024-01-01", "2024-01-04"],
            "valid": ["2024-01-05", "2024-01-06"],
            "test": ["2024-01-07", "2024-01-10"],
        },
        model_params={},
        feature_names=["signal", "noise"],
    )

    by_factor = {item["factor"]: item for item in result["features"]}
    assert result["baseline"]["rank_ic"] == pytest.approx(1.0)
    assert by_factor["signal"]["rank_ic_drop"] > 0.5
    assert by_factor["noise"]["rank_ic_drop"] == pytest.approx(0.0)
    assert result["method"]["causal_constraint"].startswith("每个交易日内")


def test_stacking_permutation_importance_uses_all_base_models(
    tmp_path: Path,
) -> None:
    dates = pd.date_range("2024-01-01", periods=10)
    instruments = [f"S{index:02d}" for index in range(10)]
    index = pd.MultiIndex.from_product(
        [dates, instruments], names=["datetime", "instrument"],
    )
    signal = np.tile(np.linspace(-1, 1, len(instruments)), len(dates))
    frame = pd.DataFrame(
        np.column_stack((signal, signal)),
        index=index,
        columns=pd.MultiIndex.from_tuples([
            ("feature", "signal"), ("label", "LABEL0"),
        ]),
    )
    dataset_path = tmp_path / "dataset.parquet"
    frame.to_parquet(dataset_path)
    model = QlibStackingModel(
        base_models=[
            {"kind": "linear", "params": {}, "model": _LinearDatasetModel()},
            {
                "kind": "random_forest", "params": {},
                "model": _LinearDatasetModel(),
            },
        ],
        meta_model=_AverageMetaModel(),
    )
    model_path = tmp_path / "model.pkl"
    model_path.write_bytes(pickle.dumps(model))
    bundle_path = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as archive:
        archive.add(model_path, arcname="model.pkl")

    result = artifact_model_permutation_importance(
        bundle_path,
        dataset_path,
        model_kind="stacking",
        segments={
            "train": ["2024-01-01", "2024-01-04"],
            "valid": ["2024-01-05", "2024-01-06"],
            "test": ["2024-01-07", "2024-01-10"],
        },
        model_params={
            "base_models": [
                {"kind": "linear", "params": {}},
                {"kind": "random_forest", "params": {}},
            ],
        },
        feature_names=["signal"],
    )

    assert result["baseline"]["rank_ic"] == pytest.approx(1.0)
    assert result["features"][0]["rank_ic_drop"] > 0.5


def test_deep_permutation_diagnostics_use_short_lived_process(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = tmp_path / "bundle.tar.gz"
    dataset = tmp_path / "dataset.parquet"
    bundle.touch()
    dataset.touch()
    captured = {}

    def run(command, **kwargs):
        captured.update({"command": command, **kwargs})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"baseline": {"rank_ic": 0.05}, "features": []}),
            stderr="",
        )

    monkeypatch.setattr("factor_service.model_diagnostics.subprocess.run", run)

    result = isolated_artifact_model_permutation_importance(
        bundle,
        dataset,
        model_kind="transformer_lstm",
        segments={"test": ["2024-01-01", "2024-01-31"]},
        model_params={"lookback_window": 60},
        feature_names=["momentum"],
    )

    payload = json.loads(captured["input"])
    assert captured["command"][-1] == "factor_service.model_diagnostics_cli"
    assert payload["model_kind"] == "transformer_lstm"
    assert payload["feature_names"] == ["momentum"]
    assert result["baseline"]["rank_ic"] == 0.05
