from types import SimpleNamespace
from pathlib import Path
from hashlib import sha256
import json
import pickle
import subprocess
import sys
import tarfile
import textwrap

import numpy as np
import pandas as pd

from factor_service.research.dataset import PreparedDataset
from factor_service.research.models import _validation_indices
from factor_service.research.preprocessing import normalize_feature_preprocessing
from factor_service.research.trainer import (
    QlibStackingModel,
    _create_model,
    _feature_importance,
    _fit_stacking,
    _fit_model,
    _incremental_prepared_dataset,
    _load_incremental_source,
    _metrics,
    _predict_dataset,
    _predict_training_dataset,
    _prepare_recorder_experiment,
    _prediction_frame,
    _qlib_lgb_params,
    _suggest_tree_hyperparameters,
    _tune_tree_hyperparameters,
    _walk_forward_frame,
    predict_feature_frame,
)


class _RecordingTrial:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def suggest_float(self, name, low, high, *, log=False):
        self.calls.append(("float", name, low, high, log))
        return low

    def suggest_int(self, name, low, high):
        self.calls.append(("int", name, low, high))
        return low


def test_optuna_tree_search_spaces_match_quantmind_ranges() -> None:
    lightgbm_trial = _RecordingTrial()
    xgboost_trial = _RecordingTrial()
    catboost_trial = _RecordingTrial()

    lightgbm = _suggest_tree_hyperparameters(lightgbm_trial, "lightgbm")
    xgboost = _suggest_tree_hyperparameters(xgboost_trial, "xgboost")
    catboost = _suggest_tree_hyperparameters(catboost_trial, "catboost")

    assert set(lightgbm) == {
        "learning_rate", "num_leaves", "min_child_samples",
        "feature_fraction", "bagging_fraction", "lambda_l1", "lambda_l2",
    }
    assert set(xgboost) == {
        "learning_rate", "max_depth", "subsample", "colsample_bytree",
        "min_child_weight", "reg_alpha",
    }
    assert set(catboost) == {
        "learning_rate", "depth", "l2_leaf_reg", "random_strength",
    }
    assert ("float", "learning_rate", 0.005, 0.1, True) in lightgbm_trial.calls
    assert ("int", "max_depth", 3, 8) in xgboost_trial.calls
    assert ("int", "depth", 4, 10) in catboost_trial.calls


def test_optuna_runs_trials_and_returns_auditable_best_params(monkeypatch) -> None:
    class _FakeDataHandler:
        @staticmethod
        def from_df(frame):
            assert not frame.empty
            return "handler"

    monkeypatch.setattr(
        "factor_service.research.trainer._dataset_for_model",
        lambda handler, segments, model_kind, params, DatasetH: {
            "params": params,
        },
    )
    monkeypatch.setattr(
        "factor_service.research.trainer._create_model",
        lambda model_kind, params, feature_count: ({"params": params}, params),
    )
    monkeypatch.setattr(
        "factor_service.research.trainer._fit_model",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "factor_service.research.trainer._predict_dataset",
        lambda *args, **kwargs: pd.Series([0.1, 0.2]),
    )
    monkeypatch.setattr(
        "factor_service.research.trainer._metrics",
        lambda *args, **kwargs: {
            "test_rows": 2,
            "test_days": 1,
            "rmse": 0.1,
            "rank_ic": 0.2,
            "rank_icir": 0.3,
        },
    )
    frame = pd.DataFrame({("feature", "f1"): [1.0, 2.0]})
    prepared = PreparedDataset(
        frame=frame,
        segments={
            "train": ("2024-01-02", "2024-01-02"),
            "valid": ("2024-01-03", "2024-01-03"),
            "test": ("2024-01-04", "2024-01-04"),
        },
        feature_names=["f1"],
        coverage={},
        medians={},
        manifest={},
    )
    progress_events: list[tuple[str, int, dict]] = []

    result = _tune_tree_hyperparameters(
        prepared,
        model_kind="lightgbm",
        base_params={"n_estimators": 5},
        config={"n_trials": 2, "seed": 7},
        DataHandlerLP=_FakeDataHandler,
        DatasetH=object,
        classification=False,
        cancellation=None,
        progress=lambda stage, percent, details: progress_events.append(
            (stage, percent, details)
        ),
    )

    assert result["completed_trials"] == 2
    assert result["best_value"] == 0.3
    assert result["best_params"]
    assert len(result["trials"]) == 2
    assert all(trial["validation"]["rank_icir"] == 0.3 for trial in result["trials"])
    assert progress_events[-1][0] == "optuna_completed"


def test_optuna_v2_scores_multiple_validation_windows_and_seeds(monkeypatch) -> None:
    class _FakeDataHandler:
        @staticmethod
        def from_df(frame):
            return "handler"

    fitted_seeds: list[int] = []
    measured_segments: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "factor_service.research.trainer._dataset_for_model",
        lambda handler, segments, model_kind, params, DatasetH: {
            "params": params,
        },
    )
    monkeypatch.setattr(
        "factor_service.research.trainer._create_model",
        lambda model_kind, params, feature_count: ({"params": params}, params),
    )
    monkeypatch.setattr(
        "factor_service.research.trainer._fit_model",
        lambda model_kind, model, *args, **kwargs: fitted_seeds.append(
            model["params"]["seed"]
        ),
    )
    monkeypatch.setattr(
        "factor_service.research.trainer._predict_dataset",
        lambda *args, **kwargs: pd.Series(dtype=float),
    )

    def fake_metrics(prediction, frame, segment, **kwargs):
        measured_segments.append(segment)
        return {
            "test_rows": 8,
            "test_days": 4,
            "rmse": 0.1,
            "rank_ic": 0.2,
            "rank_icir": 0.3,
        }

    monkeypatch.setattr("factor_service.research.trainer._metrics", fake_metrics)
    dates = pd.date_range("2024-01-02", periods=8, freq="D")
    index = pd.MultiIndex.from_product(
        [dates, ["000001.SZ"]], names=["datetime", "instrument"],
    )
    columns = pd.MultiIndex.from_tuples([
        ("feature", "f1"), ("label", "LABEL0"),
    ])
    frame = pd.DataFrame(np.ones((len(index), 2)), index=index, columns=columns)
    prepared = PreparedDataset(
        frame=frame,
        segments={
            "train": ("2024-01-02", "2024-01-02"),
            "valid": ("2024-01-02", "2024-01-09"),
            "test": ("2024-01-10", "2024-01-12"),
        },
        feature_names=["f1"],
        coverage={},
        medians={},
        manifest={},
    )

    result = _tune_tree_hyperparameters(
        prepared,
        model_kind="lightgbm",
        base_params={"n_estimators": 5},
        config={
            "n_trials": 2,
            "seed": 7,
            "validation_windows": 2,
            "seed_count": 2,
            "stability_penalty": 0.5,
            "minimum_positive_window_ratio": 0.6,
            "search_space_version": "alphablocks.tree-optuna.v2",
        },
        DataHandlerLP=_FakeDataHandler,
        DatasetH=object,
        classification=False,
        cancellation=None,
        progress=None,
    )

    assert result["schema_version"] == "alphablocks.optuna-search-result.v2"
    assert result["validation_windows"] == 2
    assert result["seed_count"] == 2
    assert result["best_value"] == 0.3
    assert fitted_seeds == [7, 8, 7, 8]
    assert prepared.segments["test"] not in measured_segments
    assert all(
        len(seed_result["windows"]) == 2
        for trial in result["trials"]
        for seed_result in trial["validation"]["validation_windows"]
    )


def test_incremental_bundle_uses_recorded_training_identity_after_registration(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "source-bundle.tar.gz"
    manifest_path = tmp_path / "manifest.json"
    model_path = tmp_path / "model.pkl"
    manifest_path.write_text(json.dumps({
        "model_id": "temporary-model",
        "model_version": 1,
        "model_kind": "lightgbm",
    }), encoding="utf-8")
    model_path.write_bytes(pickle.dumps({"model": "test"}))
    with tarfile.open(bundle_path, "w:gz") as archive:
        archive.add(manifest_path, arcname="manifest.json")
        archive.add(model_path, arcname="model.pkl")

    model, manifest = _load_incremental_source(
        SimpleNamespace(model_artifacts_root=tmp_path),
        {
            "source_model_id": "public-model",
            "source_model_version": 7,
            "source_bundle_identity": {
                "model_id": "temporary-model",
                "model_version": 1,
                "job_id": "job-source",
            },
            "source_artifact": {
                "relative_path": bundle_path.name,
                "sha256": sha256(bundle_path.read_bytes()).hexdigest(),
            },
        },
    )

    assert model == {"model": "test"}
    assert manifest["model_id"] == "temporary-model"


def test_stacking_bundle_combines_base_predictions_and_round_trips() -> None:
    from sklearn.linear_model import Ridge

    meta = Ridge(alpha=1.0).fit(
        np.asarray([[0.0, 1.0], [1.0, 0.0], [1.0, 1.0]]),
        np.asarray([0.25, 0.75, 1.0]),
    )
    model = QlibStackingModel(
        base_models=[
            {"kind": "lightgbm", "params": {}, "model": object()},
            {"kind": "linear", "params": {}, "model": object()},
        ],
        meta_model=meta,
    )
    expected = model.combine([
        np.asarray([0.2, 0.8]), np.asarray([0.6, 0.4]),
    ])
    restored = pickle.loads(pickle.dumps(model))

    assert np.allclose(restored.combine([
        np.asarray([0.2, 0.8]), np.asarray([0.6, 0.4]),
    ]), expected)


def test_remote_runtime_overrides_default_threads_and_enables_lgb_gpu(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALPHA_EFFECTIVE_NUM_THREADS", "24")
    monkeypatch.setenv("ALPHA_MODEL_ACCELERATOR", "cuda")

    params = _qlib_lgb_params({"num_threads": 4})

    assert params["num_threads"] == 24
    assert params["device_type"] == "gpu"
    assert params["gpu_use_dp"] is False


def test_catboost_translates_od_wait_without_passing_conflicting_aliases() -> None:
    model, params = _create_model(
        "catboost",
        {"od_wait": 37, "n_estimators": 10, "num_threads": 2},
        1,
    )

    assert "od_wait" not in params
    assert "od_wait" not in model._params
    assert model._alphablocks_early_stopping_rounds == 37


def test_sklearn_baselines_use_quantmind_defaults() -> None:
    random_forest, forest_params = _create_model("random_forest", {}, 3)
    ridge, ridge_params = _create_model("linear", {}, 3)

    assert forest_params["n_estimators"] == 300
    assert forest_params["max_depth"] == 0
    assert forest_params["max_features"] == "sqrt"
    assert random_forest.model.n_estimators == 300
    assert random_forest.model.max_depth is None
    assert random_forest.model.max_features == "sqrt"
    assert ridge_params["alpha"] == 3.0
    assert ridge_params["fit_intercept"] is True
    assert ridge.model.alpha == 3.0
    assert ridge.model.fit_intercept is True


def test_mlp_uses_quantmind_default_hidden_layer_shape() -> None:
    model, params = _create_model("mlp", {}, 3)

    assert params["hidden_layers"] == [64, 32]
    assert params["early_stopping_rounds"] == 20
    assert model.hidden_layers == (64, 32)


def test_validation_sampling_is_deterministic_and_spans_full_period() -> None:
    selected = _validation_indices(1_000, 100)

    assert len(selected) == 100
    assert selected[0] == 0
    assert selected[-1] == 999
    assert selected == _validation_indices(1_000, 100)


def test_remote_sequence_train_metrics_use_deterministic_sample(monkeypatch) -> None:
    class _SequenceModel:
        @staticmethod
        def predict_sampled(dataset, segment, *, max_rows):
            assert dataset == "dataset"
            assert segment == "train"
            assert max_rows == 200_000
            return pd.Series([0.1, 0.2])

    monkeypatch.setenv("ALPHA_TRAIN_METRIC_SAMPLE_ROWS", "200000")

    prediction = _predict_training_dataset(
        _SequenceModel(), "lstm", "dataset", "train", classification=False,
    )

    assert prediction.tolist() == [0.1, 0.2]


def test_stacking_fits_time_ordered_oof_and_returns_one_model() -> None:
    from qlib.data.dataset import DataHandlerLP, DatasetH

    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2024-01-02", periods=60)
    index = pd.MultiIndex.from_product(
        [dates, [f"stock-{i}" for i in range(6)]],
        names=["datetime", "instrument"],
    )
    f1 = rng.normal(size=len(index))
    f2 = rng.normal(size=len(index))
    label = 0.7 * f1 - 0.2 * f2 + rng.normal(scale=0.05, size=len(index))
    frame = pd.DataFrame(
        np.column_stack([f1, f2, label]),
        index=index,
        columns=pd.MultiIndex.from_tuples([
            ("feature", "f1"), ("feature", "f2"), ("label", "LABEL0"),
        ]),
    )
    prepared = PreparedDataset(
        frame=frame,
        segments={
            "train": (dates[0].date().isoformat(), dates[43].date().isoformat()),
            "valid": (dates[44].date().isoformat(), dates[51].date().isoformat()),
            "test": (dates[52].date().isoformat(), dates[-1].date().isoformat()),
        },
        feature_names=["f1", "f2"],
        coverage={},
        medians={"f1": 0.0, "f2": 0.0},
        manifest={},
    )
    result = _fit_stacking(
        prepared,
        model_spec={
            "kind": "stacking",
            "params": {"n_folds": 2, "meta_alpha": 1.0},
            "base_models": [
                {"kind": "linear", "params": {"alpha": 1.0}},
                {"kind": "random_forest", "params": {
                    "n_estimators": 5, "max_depth": 3, "num_threads": 1,
                }},
            ],
        },
        DataHandlerLP=DataHandlerLP,
        DatasetH=DatasetH,
        classification=False,
        cancellation=None,
        progress=None,
    )

    assert isinstance(result["model"], QlibStackingModel)
    assert result["model_params"]["oof_rows"] >= 100
    assert list(item["kind"] for item in result["model"].base_models) == [
        "linear", "random_forest",
    ]
    assert not result["valid_prediction"].empty
    assert not result["test_prediction"].empty


def test_qlib_column_feature_importance_maps_back_to_frozen_factor_names() -> None:
    class _Model:
        @staticmethod
        def get_feature_importance():
            return pd.Series({"Column_0": 12, "Column_1": 7, "Column_2": 3})

    rows = _feature_importance(_Model(), ["amount", "turnover", "momentum"])

    assert rows == [
        {"factor": "amount", "importance": 12.0, "rank": 1},
        {"factor": "turnover", "importance": 7.0, "rank": 2},
        {"factor": "momentum", "importance": 3.0, "rank": 3},
    ]


def test_catboost_fit_copies_framework_history_into_shared_evaluations() -> None:
    class _RawModel:
        @staticmethod
        def get_evals_result():
            return {
                "learn": {"RMSE": [0.5, 0.4]},
                "validation": {"RMSE": [0.52, 0.43]},
            }

    class _Model:
        _alphablocks_num_boost_round = 10
        _alphablocks_early_stopping_rounds = 2
        model = _RawModel()

        @staticmethod
        def fit(*_args, **_kwargs):
            return None

    evaluations = {}
    _fit_model(
        "catboost", _Model(), object(), evaluations,
        cancellation=None, progress=None,
    )

    assert evaluations == {
        "train": [0.5, 0.4],
        "valid": [0.52, 0.43],
    }


def test_catboost_binary_prediction_supports_qlib_base_model() -> None:
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-01-02"), "SH600000"),
         (pd.Timestamp("2026-01-02"), "SZ000001")],
        names=["datetime", "instrument"],
    )
    features = pd.DataFrame({"factor": [0.1, 0.2]}, index=index)

    class _Predictor:
        @staticmethod
        def predict(values, *, prediction_type=None):
            assert values.shape == (2, 1)
            assert prediction_type == "Probability"
            return np.asarray([[0.8, 0.2], [0.3, 0.7]])

    class _Model:
        model = _Predictor()
        _alphablocks_loss = "binary"

    class _Dataset:
        @staticmethod
        def prepare(*_args, **_kwargs):
            return features

    prediction = _predict_dataset(
        _Model(), "catboost", _Dataset(), "test", classification=True,
    )

    assert prediction.index.equals(index)
    assert prediction.tolist() == [0.2, 0.7]
    assert predict_feature_frame(_Model(), "catboost", features).tolist() == [0.2, 0.7]


def test_recorder_experiment_uses_job_local_artifact_root(tmp_path: Path) -> None:
    from mlflow.tracking import MlflowClient

    recorder_uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    recorder_root = tmp_path / "mlruns"

    _prepare_recorder_experiment(recorder_uri, "local_recorder", recorder_root)

    experiment = MlflowClient(tracking_uri=recorder_uri).get_experiment_by_name(
        "local_recorder"
    )
    assert experiment is not None
    assert experiment.artifact_location == recorder_root.resolve().as_uri()


def test_lightgbm_parameters_are_deterministic() -> None:
    params = _qlib_lgb_params({"n_estimators": 123, "num_threads": 2})
    assert params["num_boost_round"] == 123
    assert params["seed"] == 42
    assert params["deterministic"] is True
    assert params["num_threads"] == 2
    assert params["metric"] == "rmse"

    repeated = _qlib_lgb_params({"seed": 43})
    assert repeated["seed"] == 43
    assert repeated["feature_fraction_seed"] == 43
    assert repeated["bagging_seed"] == 43
    assert repeated["data_random_seed"] == 43

    binary = _qlib_lgb_params({"loss": "binary", "metric": "binary_logloss"})
    assert binary["loss"] == "binary"
    assert binary["metric"] == "binary_logloss"


def test_lightgbm_parameters_match_quantmind_defaults() -> None:
    params = _qlib_lgb_params({})

    assert params["learning_rate"] == 0.02
    assert params["min_data_in_leaf"] == 300
    assert params["min_child_samples"] == 150
    assert params["path_smooth"] == 1.0
    assert params["bagging_freq"] == 5
    assert params["lambda_l1"] == 0.5
    assert params["lambda_l2"] == 1.0
    assert params["feature_fraction"] == 0.7
    assert params["bagging_fraction"] == 0.8


def test_classification_metrics_use_probability_outputs() -> None:
    index = pd.MultiIndex.from_tuples([
        (pd.Timestamp("2024-01-02"), "A"),
        (pd.Timestamp("2024-01-02"), "B"),
        (pd.Timestamp("2024-01-03"), "A"),
        (pd.Timestamp("2024-01-03"), "B"),
    ], names=["datetime", "instrument"])
    frame = pd.DataFrame(
        [0.0, 1.0, 0.0, 1.0], index=index,
        columns=pd.MultiIndex.from_tuples([("label", "LABEL0")]),
    )
    prediction = pd.Series([0.1, 0.9, 0.2, 0.8], index=index)

    metrics = _metrics(
        prediction, frame, ("2024-01-02", "2024-01-03"), classification=True,
    )

    assert metrics["auc"] == 1.0
    assert metrics["accuracy"] == 1.0
    assert metrics["f1"] == 1.0
    assert np.isclose(metrics["mae"], 0.15)
    assert np.isclose(metrics["mse"], 0.025)
    assert metrics["rank_icir"] == metrics["ic_ir"]
    assert 0.0 < metrics["log_loss"] < 1.0


def test_incremental_dataset_uses_only_new_dates_and_source_medians() -> None:
    dates = pd.date_range("2024-07-01", periods=80, freq="B")
    index = pd.MultiIndex.from_product(
        [dates, ["A", "B"]], names=["datetime", "instrument"],
    )
    raw = pd.DataFrame(
        {
            ("feature", "factor__v1__hash"): np.where(
                np.arange(len(index)) % 7 == 0, np.nan, 2.0,
            ),
            ("label", "LABEL0"): np.linspace(-1.0, 1.0, len(index)),
        },
        index=index,
    )
    raw.columns = pd.MultiIndex.from_tuples(raw.columns)
    prepared = PreparedDataset(
        frame=raw.fillna(9.0),
        segments={
            "train": ("2024-07-01", "2024-08-01"),
            "valid": ("2024-08-05", "2024-08-20"),
            "test": ("2024-08-22", "2024-10-18"),
        },
        feature_names=["factor__v1__hash"],
        coverage={"factor": 0.9},
        medians={"factor__v1__hash": 9.0},
        manifest={
            "schema_version": "dataset",
            # Exclusion metadata was introduced after legacy disabled models;
            # it has no numerical effect while preprocessing is off.
            "preprocessing_excluded_features": ["factor__v1__hash"],
        },
        raw_frame=raw,
    )

    incremental = _incremental_prepared_dataset(
        prepared,
        {
            "mode": "lightgbm_append_trees_new_data_only",
            "source_model_id": "demo",
            "source_model_version": 1,
            "source_date_end": "2024-06-28",
            "minimum_new_trading_sessions": 60,
        },
        {
            "feature_names": ["factor__v1__hash"],
            "medians": {"factor__v1__hash": 0.5},
        },
        horizon=5,
    )

    assert incremental.frame.index.get_level_values("datetime").min() > pd.Timestamp("2024-06-28")
    assert incremental.frame[("feature", "factor__v1__hash")].isna().sum() == 0
    assert 0.5 in incremental.frame[("feature", "factor__v1__hash")].values
    assert incremental.medians == {"factor__v1__hash": 0.5}
    assert incremental.manifest["incremental_training"]["new_trading_sessions"] == 80


def test_lightgbm_incremental_fit_appends_trees_to_source_booster() -> None:
    script = """
        import numpy as np
        import pandas as pd
        from qlib.data.dataset import DataHandlerLP, DatasetH
        from qlib.workflow import R
        from factor_service.research.trainer import _create_model, _fit_model

        R.log_metrics = lambda **kwargs: None
        dates = pd.date_range("2024-01-02", periods=80, freq="B")
        index = pd.MultiIndex.from_product(
            [dates, ["A", "B"]], names=["datetime", "instrument"],
        )
        rng = np.random.default_rng(42)
        feature = rng.normal(size=len(index))
        frame = pd.DataFrame(
            np.column_stack((feature, feature * 0.4 + rng.normal(scale=0.1, size=len(index)))),
            index=index,
            columns=pd.MultiIndex.from_tuples([
                ("feature", "f1"), ("label", "LABEL0"),
            ]),
        )
        dataset = DatasetH(handler=DataHandlerLP.from_df(frame), segments={
            "train": (dates[0].date().isoformat(), dates[49].date().isoformat()),
            "valid": (dates[50].date().isoformat(), dates[64].date().isoformat()),
            "test": (dates[65].date().isoformat(), dates[-1].date().isoformat()),
        })
        source, _ = _create_model("lightgbm", {
            "n_estimators": 5, "early_stopping_rounds": 2, "num_threads": 1,
            "min_data_in_leaf": 2, "min_child_samples": 2,
        }, 1)
        _fit_model("lightgbm", source, dataset, {}, cancellation=None, progress=None)
        source_trees = source.model.num_trees()
        updated, _ = _create_model("lightgbm", {
            "n_estimators": 3, "early_stopping_rounds": 2, "num_threads": 1,
            "min_data_in_leaf": 2, "min_child_samples": 2,
        }, 1)
        _fit_model(
            "lightgbm", updated, dataset, {}, cancellation=None, progress=None,
            initial_model=source,
        )
        assert updated.model.num_trees() > source_trees
    """
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_lightgbm_zero_validation_trains_fixed_rounds_without_valid_metrics() -> None:
    script = """
        import numpy as np
        import pandas as pd
        from qlib.data.dataset import DataHandlerLP, DatasetH
        from qlib.workflow import R
        from factor_service.research.trainer import _create_model, _fit_model

        R.log_metrics = lambda **kwargs: None
        dates = pd.date_range("2024-01-02", periods=60, freq="B")
        index = pd.MultiIndex.from_product(
            [dates, ["A", "B"]], names=["datetime", "instrument"],
        )
        rng = np.random.default_rng(7)
        feature = rng.normal(size=len(index))
        frame = pd.DataFrame(
            np.column_stack((feature, feature * 0.3 + rng.normal(scale=0.2, size=len(index)))),
            index=index,
            columns=pd.MultiIndex.from_tuples([
                ("feature", "f1"), ("label", "LABEL0"),
            ]),
        )
        segments = {
            "train": (dates[0].date().isoformat(), dates[39].date().isoformat()),
            "valid": (dates[0].date().isoformat(), dates[39].date().isoformat()),
            "test": (dates[45].date().isoformat(), dates[-1].date().isoformat()),
        }
        dataset = DatasetH(handler=DataHandlerLP.from_df(frame), segments=segments)
        dataset._alphablocks_validation_enabled = False
        model, _ = _create_model("lightgbm", {
            "n_estimators": 7, "early_stopping_rounds": 0, "num_threads": 1,
            "min_data_in_leaf": 2, "min_child_samples": 2,
        }, 1)
        evaluations = {}
        _fit_model(
            "lightgbm", model, dataset, evaluations,
            cancellation=None, progress=None,
        )
        assert model.model.num_trees() == 7
        assert set(evaluations) == {"train"}
    """
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_new_model_kinds_instantiate_and_pickle() -> None:
    scenarios = {
        "random_forest": {"n_estimators": 10, "max_depth": 3, "num_threads": 2},
        "linear": {"alpha": 1.0},
        "gru": {
            "lookback_window": 8, "hidden_size": 16, "num_layers": 1,
            "max_steps": 2, "batch_size": 64,
        },
        "alstm": {
            "lookback_window": 8, "hidden_size": 16, "num_layers": 1,
            "max_steps": 2, "batch_size": 64,
        },
        "transformer": {
            "lookback_window": 8, "d_model": 16, "nhead": 4,
            "transformer_layers": 1, "max_steps": 2, "batch_size": 64,
        },
        "tabnet": {
            "n_d": 8, "n_a": 8, "n_steps": 2, "max_steps": 2,
            "batch_size": 64, "pretrain": False,
        },
        "tcn": {
            "lookback_window": 8, "hidden_size": 16, "kernel_size": 3,
            "num_layers": 2, "max_steps": 2, "batch_size": 64,
        },
        "nativetft": {
            "lookback_window": 8, "d_model": 16, "nhead": 4,
            "gru_hidden_size": 16, "num_layers": 1,
            "max_steps": 2, "batch_size": 64,
        },
        "transformer_lstm": {
            "lookback_window": 8, "d_model": 16, "nhead": 4,
            "transformer_layers": 1, "max_steps": 2, "batch_size": 64,
        },
    }
    for kind, params in scenarios.items():
        model, _ = _create_model(kind, params, 8)
        restored = pickle.loads(pickle.dumps(model))
        assert restored.__class__ is model.__class__


def test_supported_model_factories_are_available() -> None:
    for kind in ("lightgbm", "xgboost", "catboost", "mlp", "lstm", "transformer_lstm"):
        completed = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(f"""
                from factor_service.research.trainer import _create_model
                model, params = _create_model(
                    {kind!r}, {{"n_estimators": 2, "max_steps": 2, "batch_size": 16}}, 3,
                )
                assert model is not None and params
            """)],
            check=False, capture_output=True, text=True,
        )
        assert completed.returncode == 0, completed.stderr


def test_custom_mlp_fits_qlib_dataseth_and_predicts_frame() -> None:
    script = """
        import numpy as np
        import pandas as pd
        from qlib.data.dataset import DataHandlerLP, DatasetH
        from factor_service.research.trainer import _create_model, predict_feature_frame
        index = pd.MultiIndex.from_product(
            [pd.date_range("2024-01-01", periods=8), ["A", "B"]],
            names=["datetime", "instrument"],
        )
        values = np.random.default_rng(42).normal(size=(len(index), 3))
        frame = pd.DataFrame(
            np.column_stack((values, values[:, 0] - values[:, 1])), index=index,
            columns=pd.MultiIndex.from_tuples([
                ("feature", "f1"), ("feature", "f2"), ("feature", "f3"),
                ("label", "LABEL0"),
            ]),
        )
        dataset = DatasetH(handler=DataHandlerLP.from_df(frame), segments={
            "train": ("2024-01-01", "2024-01-04"),
            "valid": ("2024-01-05", "2024-01-06"),
            "test": ("2024-01-07", "2024-01-08"),
        })
        model, _ = _create_model("mlp", {
            "max_steps": 3, "batch_size": 4, "eval_steps": 1,
            "early_stopping_rounds": 2, "num_threads": 1,
        }, 3)
        model.fit(dataset, evals_result={})
        test = dataset.prepare("test", col_set="feature", data_key=DataHandlerLP.DK_I)
        prediction = predict_feature_frame(model, "mlp", test)
        assert prediction.shape == (len(test),)
        assert np.isfinite(prediction).all()
    """
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_custom_mlp_builds_each_configured_hidden_layer_width() -> None:
    script = """
        from factor_service.research.trainer import _create_model
        model, params = _create_model("mlp", {
            "hidden_layers": [64, 128, 256], "max_steps": 2, "batch_size": 16,
        }, 42)
        linear = [layer for layer in model.network if layer.__class__.__name__ == "Linear"]
        assert [(layer.in_features, layer.out_features) for layer in linear] == [
            (42, 64), (64, 128), (128, 256), (256, 1),
        ]
        assert params["hidden_layers"] == [64, 128, 256]
    """
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_custom_lstm_fits_causal_qlib_time_windows() -> None:
    script = """
        import pickle
        import numpy as np
        import pandas as pd
        from qlib.data.dataset import DataHandlerLP, TSDatasetH
        from factor_service.research.trainer import _create_model
        index = pd.MultiIndex.from_product(
            [pd.date_range("2024-01-01", periods=12), ["A", "B"]],
            names=["datetime", "instrument"],
        )
        values = np.random.default_rng(42).normal(size=(len(index), 3))
        frame = pd.DataFrame(
            np.column_stack((values, values[:, 0] - values[:, 1])), index=index,
            columns=pd.MultiIndex.from_tuples([
                ("feature", "f1"), ("feature", "f2"), ("feature", "f3"),
                ("label", "LABEL0"),
            ]),
        )
        dataset = TSDatasetH(
            handler=DataHandlerLP.from_df(frame),
            segments={
                "train": ("2024-01-03", "2024-01-06"),
                "valid": ("2024-01-07", "2024-01-09"),
                "test": ("2024-01-10", "2024-01-12"),
            },
            step_len=3,
        )
        model, params = _create_model("lstm", {
            "lookback_window": 3, "hidden_size": 8, "num_layers": 1,
            "dropout": 0.0, "max_steps": 3, "batch_size": 16,
            "eval_steps": 1, "early_stopping_rounds": 2,
        }, 3)
        model.fit(dataset, evals_result={})
        restored = pickle.loads(pickle.dumps(model))
        prediction = restored.predict(dataset, segment="test")
        assert len(prediction) == 6
        assert prediction.index.names == ["datetime", "instrument"]
        assert np.isfinite(prediction.values).all()
        assert params["lookback_window"] == 3
    """
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_transformer_lstm_fits_the_same_causal_time_windows() -> None:
    script = """
        import pickle
        import numpy as np
        import pandas as pd
        from qlib.data.dataset import DataHandlerLP, TSDatasetH
        from factor_service.research.trainer import _create_model
        index = pd.MultiIndex.from_product(
            [pd.date_range("2024-01-01", periods=12), ["A", "B"]],
            names=["datetime", "instrument"],
        )
        values = np.random.default_rng(42).normal(size=(len(index), 3))
        frame = pd.DataFrame(
            np.column_stack((values, values[:, 0] - values[:, 1])), index=index,
            columns=pd.MultiIndex.from_tuples([
                ("feature", "f1"), ("feature", "f2"), ("feature", "f3"),
                ("label", "LABEL0"),
            ]),
        )
        dataset = TSDatasetH(
            handler=DataHandlerLP.from_df(frame),
            segments={
                "train": ("2024-01-03", "2024-01-06"),
                "valid": ("2024-01-07", "2024-01-09"),
                "test": ("2024-01-10", "2024-01-12"),
            },
            step_len=3,
        )
        model, params = _create_model("transformer_lstm", {
            "lookback_window": 3, "d_model": 8, "nhead": 2,
            "transformer_layers": 1, "dim_feedforward": 16,
            "lstm_hidden_size": 8, "lstm_layers": 1, "dropout": 0.0,
            "max_steps": 2, "batch_size": 16, "eval_steps": 1,
            "early_stopping_rounds": 2,
        }, 3)
        model.fit(dataset, evals_result={})
        restored = pickle.loads(pickle.dumps(model))
        prediction = restored.predict(dataset, segment="test")
        assert len(prediction) == 6
        assert np.isfinite(prediction.values).all()
        assert params["d_model"] == 8 and params["nhead"] == 2
        assert len(restored.get_feature_importance()) == 3
    """
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_prediction_rank_one_is_the_highest_score() -> None:
    index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2024-01-02"), "LOW"),
            (pd.Timestamp("2024-01-02"), "HIGH"),
        ],
        names=["datetime", "instrument"],
    )
    prediction = pd.Series([0.1, 0.9], index=index)
    prepared = SimpleNamespace()
    frame = _prediction_frame(prediction, prepared, {"job_id": "job-1"})
    ranked = frame.set_index("entity_code")

    assert ranked.loc["HIGH", "rank_value"] == 1
    assert ranked.loc["HIGH", "score"] == 1.0
    assert ranked.loc["LOW", "score"] == 0.0
    assert str(ranked.loc["HIGH", "feature_cutoff_at"].tz) == "Asia/Shanghai"
    assert ranked.loc["HIGH", "feature_cutoff_at"].hour == 15
    assert str(ranked.loc["HIGH", "computed_at"].tz) == "Asia/Shanghai"


def test_industry_prediction_uses_full_minus_one_to_one_range() -> None:
    index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2024-01-02"), "801010.SI"),
            (pd.Timestamp("2024-01-02"), "801020.SI"),
            (pd.Timestamp("2024-01-02"), "801030.SI"),
        ],
        names=["datetime", "instrument"],
    )
    prediction = pd.Series([0.9, 0.5, 0.1], index=index)
    job = {
        "job_id": "industry-job",
        "dataset_spec": {"prediction_scope": "industry"},
    }

    ranked = _prediction_frame(
        prediction, SimpleNamespace(), job,
    ).set_index("entity_code")

    assert ranked.loc["801010.SI", "score"] == 1.0
    assert ranked.loc["801020.SI", "score"] == 0.0
    assert ranked.loc["801030.SI", "score"] == -1.0


def test_walk_forward_imputation_is_fitted_on_window_train_only() -> None:
    dates = pd.date_range("2024-01-02", periods=12, freq="B")
    index = pd.MultiIndex.from_product(
        [dates, ["A"]], names=["datetime", "instrument"],
    )
    values = [1.0] * 5 + [999.0] * 6 + [float("nan")]
    raw = pd.DataFrame(
        {("feature", "factor"): values, ("label", "LABEL0"): [0.0] * 12},
        index=index,
    )
    prepared = SimpleNamespace(raw_frame=raw, feature_names=["factor"])
    segments = {
        "train": (dates[0].date().isoformat(), dates[4].date().isoformat()),
        "valid": (dates[6].date().isoformat(), dates[7].date().isoformat()),
        "test": (dates[9].date().isoformat(), dates[-1].date().isoformat()),
    }

    frame, medians = _walk_forward_frame(prepared, segments)

    assert medians["factor"] == 1.0
    assert frame.loc[(dates[-1], "A"), ("feature", "factor")] == 1.0


def test_walk_forward_reuses_prelabel_daily_cross_section() -> None:
    dates = pd.date_range("2024-01-02", periods=12, freq="B")
    index = pd.MultiIndex.from_product(
        [dates, ["A", "B"]], names=["datetime", "instrument"],
    )
    raw = pd.DataFrame(
        {
            ("feature", "factor"): np.tile([0.0, 100.0], len(dates)),
            ("label", "LABEL0"): 0.0,
        },
        index=index,
    )
    frozen = raw.copy()
    frozen[("feature", "factor")] = np.tile([-1.0, 1.0], len(dates))
    prepared = PreparedDataset(
        frame=frozen,
        segments={},
        feature_names=["factor"],
        coverage={},
        medians={"factor": 50.0},
        manifest={
            "preprocessing": normalize_feature_preprocessing(
                {"enabled": True}, default_enabled=False,
            ),
            "preprocessing_excluded_features": [],
        },
        raw_frame=raw,
    )
    segments = {
        "train": (dates[0].date().isoformat(), dates[4].date().isoformat()),
        "valid": (dates[6].date().isoformat(), dates[7].date().isoformat()),
        "test": (dates[9].date().isoformat(), dates[-1].date().isoformat()),
    }

    frame, _medians = _walk_forward_frame(prepared, segments)

    pd.testing.assert_series_equal(
        frame[("feature", "factor")],
        frozen.loc[pd.IndexSlice[dates[0]:dates[-1], :], ("feature", "factor")],
    )


def test_walk_forward_lightgbm_produces_real_oos_predictions() -> None:
    script = """
        import os
        import tempfile
        from pathlib import Path
        import numpy as np
        import pandas as pd
        import qlib
        from qlib.data.dataset import DataHandlerLP, DatasetH
        from factor_service.research.dataset import PreparedDataset
        from factor_service.research.trainer import (
            _prepare_recorder_experiment,
            _run_walk_forward,
        )

        root = Path(tempfile.mkdtemp())
        os.chdir(root)
        dates = pd.date_range("2020-01-02", periods=380, freq="B")
        index = pd.MultiIndex.from_product(
            [dates, ["A", "B", "C", "D"]], names=["datetime", "instrument"],
        )
        rng = np.random.default_rng(42)
        feature = rng.normal(size=len(index))
        label = feature * 0.5 + rng.normal(scale=0.1, size=len(index))
        raw = pd.DataFrame(
            {("feature", "factor"): feature, ("label", "LABEL0"): label},
            index=index,
        )
        prepared = PreparedDataset(
            frame=raw, segments={}, feature_names=["factor"], coverage={},
            medians={}, manifest={}, raw_frame=raw,
        )
        provider = root / "provider"
        provider.mkdir()
        qlib.init(provider_uri=str(provider), expression_cache=None, dataset_cache=None)
        recorder_uri = f"sqlite:///{(root / 'mlflow.db').as_posix()}"
        _prepare_recorder_experiment(
            recorder_uri, "walk_forward_test", root / "mlruns",
        )
        result = _run_walk_forward(
            prepared,
            {
                "enabled": True, "strategy": "rolling", "train_sessions": 252,
                "valid_sessions": 21, "test_sessions": 21, "step_sessions": 21,
                "embargo_sessions": 5,
                "oos_date_start": dates[300].date().isoformat(),
                "oos_date_end": dates[320].date().isoformat(),
            },
            work_dir=root, model_id="walk_forward_test", model_version=1,
            model_kind="lightgbm",
            raw_params={"n_estimators": 2, "early_stopping_rounds": 1, "num_threads": 1},
            DataHandlerLP=DataHandlerLP, DatasetH=DatasetH,
            recorder_uri=recorder_uri, experiment_name="walk_forward_test",
            cancellation=None, progress=None,
        )
        prediction = result.prediction
        report = result.report
        assert len(prediction) == 84
        assert not prediction.index.duplicated().any()
        assert report["window_count"] == 1
        assert report["aggregate"]["test_days"] == 21
        assert report["stability"]["status"] == "insufficient_windows"
        assert report["stability"]["passed"] is False
        assert report["orchestration"]["task_generator"].endswith("RollingGen")
        assert report["orchestration"]["trainer"].endswith("TrainerR")
        assert report["windows"][0]["qlib_recorder_id"]
    """
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
