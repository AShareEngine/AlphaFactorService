from types import SimpleNamespace
import subprocess
import sys
import textwrap

import pandas as pd

from factor_service.research.trainer import (
    _create_model,
    _prediction_frame,
    _qlib_lgb_params,
    predict_feature_frame,
)


def test_lightgbm_parameters_are_deterministic() -> None:
    params = _qlib_lgb_params({"n_estimators": 123, "num_threads": 2})
    assert params["num_boost_round"] == 123
    assert params["seed"] == 42
    assert params["deterministic"] is True
    assert params["num_threads"] == 2


def test_supported_model_factories_are_available() -> None:
    for kind in ("lightgbm", "xgboost", "catboost", "mlp"):
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
