from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import pickle
import platform
import tarfile
from typing import Any

import clickhouse_connect
import numpy as np
import pandas as pd

from factor_service.research.config import Settings
from factor_service.research.dataset import DatasetBuilder, PreparedDataset
from factor_service.research.job import CancellationToken, ProgressCallback


@dataclass(frozen=True)
class TrainingResult:
    result: dict[str, Any]
    artifacts: list[tuple[str, Path]]
    predictions_path: Path


class QlibTrainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.dataset_builder = DatasetBuilder(settings)

    def train(
        self,
        job: dict[str, Any],
        work_dir: Path,
        *,
        cancellation: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
    ) -> TrainingResult:
        try:
            import qlib
            from qlib.data.dataset import DataHandlerLP, DatasetH
            from qlib.workflow import R
        except ImportError as exc:
            raise RuntimeError("Qlib模型环境尚未安装，请执行uv sync") from exc
        work_dir.mkdir(parents=True, exist_ok=True)
        _checkpoint(cancellation)
        prepared = self.dataset_builder.build(job, cancellation=cancellation, progress=progress)
        dataset_path = work_dir / "dataset.parquet"
        prepared.frame.to_parquet(dataset_path)
        handler = DataHandlerLP.from_df(prepared.frame)
        dataset = DatasetH(handler=handler, segments=prepared.segments)
        raw_params = dict((job.get("config_json") or {}).get("model", {}).get("params") or {})
        model_kind = str((job.get("config_json") or {}).get("model", {}).get("kind") or "lightgbm")
        model, model_params = _create_model(model_kind, raw_params, len(prepared.feature_names))
        recorder_root = work_dir / "mlruns"
        # DatasetH由已冻结的DataFrame驱动，不读取Qlib本地行情；0.9.7仍要求
        # provider_uri非空，因此给每个任务一个隔离的空Provider目录。
        provider_root = work_dir / "qlib_provider"
        provider_root.mkdir(parents=True, exist_ok=True)
        qlib.init(provider_uri=str(provider_root), expression_cache=None, dataset_cache=None)
        # MLflow 3.15开始默认拒绝新的文件目录型tracking store；SQLite既支持
        # Qlib Recorder，又能随模型产物一起打包，避免依赖外部MLflow服务。
        recorder_db = work_dir / "mlflow.db"
        recorder_uri = f"sqlite:///{recorder_db.as_posix()}"
        experiment_name = f"alphablocks_{job['model_id']}"
        evals_result: dict[str, Any] = {}
        with R.start(
            experiment_name=experiment_name,
            recorder_name=str(job["job_id"]),
            uri=recorder_uri,
        ):
            R.log_params(
                job_id=job["job_id"],
                model_id=job["model_id"],
                dataset_hash=job["dataset_hash"],
                schema_version="alphablocks.qlib-training.v1",
            )
            _progress(progress, "training", 58, {"iteration": 0})
            _fit_model(
                model_kind, model, dataset, evals_result,
                cancellation=cancellation, progress=progress,
            )
            _checkpoint(cancellation)
            _progress(progress, "predicting", 82, {})
            test_prediction = model.predict(dataset, segment="test")
            R.save_objects(trained_model=model)
            recorder_id = R.get_recorder().id
        # 研究回测只允许使用完全样本外的test段；train/valid预测不得进入模型信号库。
        prediction_frame = _prediction_frame(test_prediction, prepared, job)
        predictions_path = work_dir / "predictions.parquet"
        prediction_frame.to_parquet(predictions_path, index=False)
        prediction_rows = len(prediction_frame)
        metrics = _metrics(test_prediction, prepared.frame, prepared.segments["test"])
        feature_importance = _feature_importance(model, prepared.feature_names)
        manifest = {
            **prepared.manifest,
            "job_id": job["job_id"],
            "model_id": job["model_id"],
            "model_kind": model_kind,
            "model_version": int((job.get("config_json") or {}).get("planned_model_version") or 1),
            "qlib_recorder_id": recorder_id,
            "qlib_recorder_uri": recorder_uri,
            "model_params": model_params,
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "qlib": getattr(qlib, "__version__", "unknown"),
                **_model_package_version(model_kind),
            },
            "prediction_rows": prediction_rows,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest_path = work_dir / "manifest.json"
        metrics_path = work_dir / "metrics.json"
        importance_path = work_dir / "feature_importance.json"
        config_path = work_dir / "task_config.json"
        model_path = work_dir / "model.pkl"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        importance_path.write_text(json.dumps(feature_importance, ensure_ascii=False, indent=2), encoding="utf-8")
        config_path.write_text(json.dumps(job.get("config_json") or {}, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        with model_path.open("wb") as target:
            pickle.dump(model, target)
        bundle_path = work_dir / "qlib_experiment.tar.gz"
        with tarfile.open(bundle_path, "w:gz") as archive:
            for path in [manifest_path, metrics_path, importance_path, config_path, model_path, predictions_path]:
                archive.add(path, arcname=path.name)
            if recorder_db.exists():
                archive.add(recorder_db, arcname=recorder_db.name)
            if recorder_root.exists():
                archive.add(recorder_root, arcname="mlruns")
        result = {
            "metrics": metrics,
            "feature_importance": feature_importance,
            "predictions": {
                "row_count": prediction_rows,
                "date_start": prediction_frame["trade_date"].min().isoformat(),
                "date_end": prediction_frame["trade_date"].max().isoformat(),
                "inference_run_id": str(job["job_id"]),
                "model_version": int((job.get("config_json") or {}).get("planned_model_version") or 1),
            },
            "manifest": manifest,
        }
        artifacts = [
            ("bundle", bundle_path),
            ("dataset", dataset_path),
            ("predictions", predictions_path),
            ("manifest", manifest_path),
        ]
        _checkpoint(cancellation)
        _progress(progress, "packaged", 88, {
            "artifact_count": len(artifacts), "prediction_rows": prediction_rows,
        })
        return TrainingResult(result=result, artifacts=artifacts, predictions_path=predictions_path)

    def publish_predictions(
        self,
        path: Path,
        job: dict[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> int:
        """Idempotently publish one inference run after artifacts are durable.

        AlphaBlocks only exposes predictions through a registered model version.  The
        reserved version is removed before insertion so retries or a newly reserved job
        cannot mix two inference runs under one model version.
        """
        _checkpoint(cancellation)
        frame = pd.read_parquet(path)
        required = {
            "trade_date", "entity_code", "raw_prediction", "rank_value", "percentile",
            "score", "feature_cutoff_at", "computed_at", "source_vintage",
        }
        missing = sorted(required - set(frame.columns))
        if missing:
            raise ValueError("预测文件缺少字段: " + ", ".join(missing))
        if frame.empty:
            raise ValueError("测试段预测结果为空")
        if frame.duplicated(["trade_date", "entity_code"]).any():
            raise ValueError("预测文件包含重复的日期与股票")
        client = clickhouse_connect.get_client(
            host=self.settings.clickhouse_host,
            port=self.settings.clickhouse_port,
            username=self.settings.clickhouse_user,
            password=self.settings.clickhouse_password,
            autogenerate_session_id=False,
        )
        model_version = int((job.get("config_json") or {}).get("planned_model_version") or 1)
        dates = sorted({pd.Timestamp(value).date() for value in frame["trade_date"]})
        delete_scope = "AND trade_date IN {trade_dates:Array(Date)}" if str(job.get("kind") or "train") == "infer" else ""
        parameters = {
            "model_id": str(job["model_id"]),
            "model_version": model_version,
        }
        if delete_scope:
            parameters["trade_dates"] = dates
        client.command(
            f"""
            ALTER TABLE {self.settings.model_database}.model_predictions_daily
            DELETE WHERE model_id = {{model_id:String}}
              AND model_version = {{model_version:UInt32}}
              {delete_scope}
            SETTINGS mutations_sync = 2
            """,
            parameters=parameters,
        )
        _checkpoint(cancellation)
        now = datetime.now()
        rows = [
            [
                row.trade_date, "stock", row.entity_code, job["model_id"],
                model_version,
                row.raw_prediction, row.rank_value, row.percentile, row.score,
                row.feature_cutoff_at, row.computed_at, row.source_vintage,
                job["dataset_hash"], job["job_id"], now,
            ]
            for row in frame.itertuples(index=False)
        ]
        client.insert(
            f"{self.settings.model_database}.model_predictions_daily",
            rows,
            column_names=[
                "trade_date", "entity_type", "entity_code", "model_id",
                "model_version", "raw_prediction", "rank_value", "percentile",
                "score", "feature_cutoff_at", "computed_at", "source_vintage",
                "dataset_hash", "inference_run_id", "updated_at",
            ],
        )
        published = int(client.query(
            f"""
            SELECT count()
            FROM {self.settings.model_database}.model_predictions_daily FINAL
            WHERE model_id = {{model_id:String}}
              AND model_version = {{model_version:UInt32}}
              AND inference_run_id = {{inference_run_id:String}}
            """,
            parameters={
                "model_id": str(job["model_id"]),
                "model_version": model_version,
                "inference_run_id": str(job["job_id"]),
            },
        ).result_rows[0][0])
        if published != len(rows):
            raise ValueError(f"预测发布校验失败: 期望{len(rows)}，实际{published}")
        return published


def _fit_model(
    model_kind: str,
    model: Any,
    dataset: Any,
    evals_result: dict[str, Any],
    *,
    cancellation: CancellationToken | None,
    progress: ProgressCallback | None,
) -> None:
    """Fit one Qlib model; LightGBM retains per-iteration cancellation points."""
    if model_kind != "lightgbm":
        _checkpoint(cancellation)
        if model_kind in {"xgboost", "catboost"}:
            model.fit(
                dataset,
                num_boost_round=int(getattr(model, "_alphablocks_num_boost_round", 1000)),
                early_stopping_rounds=int(getattr(model, "_alphablocks_early_stopping_rounds", 50)),
                verbose_eval=20,
                evals_result=evals_result,
            )
        else:
            model.fit(
                dataset, evals_result=evals_result,
                cancellation=cancellation, progress=progress,
            )
        _checkpoint(cancellation)
        _progress(progress, "training", 80, {"model_kind": model_kind, "completed": True})
        return

    import lightgbm as lgb
    from qlib.workflow import R

    prepared_sets = model._prepare_data(dataset)  # Qlib's canonical DatasetH adapter.
    datasets, names = list(zip(*prepared_sets))
    total = max(1, int(model.num_boost_round))

    def cooperative_callback(environment: Any) -> None:
        _checkpoint(cancellation)
        iteration = int(environment.iteration) + 1
        if iteration == 1 or iteration % 20 == 0:
            percent = min(80, 58 + int(22 * iteration / total))
            _progress(progress, "training", percent, {"iteration": iteration, "total_iterations": total})

    cooperative_callback.order = 0  # type: ignore[attr-defined]
    cooperative_callback.before_iteration = True  # type: ignore[attr-defined]
    callbacks = [
        cooperative_callback,
        lgb.early_stopping(model.early_stopping_rounds),
        lgb.log_evaluation(period=20),
        lgb.record_evaluation(evals_result),
    ]
    model.model = lgb.train(
        model.params,
        datasets[0],
        num_boost_round=model.num_boost_round,
        valid_sets=datasets,
        valid_names=names,
        callbacks=callbacks,
    )
    for name in names:
        for metric, values in evals_result[name].items():
            for epoch, value in enumerate(values):
                R.log_metrics(**{f"{metric}.{name}".replace("@", "_"): value}, step=epoch)


def _checkpoint(cancellation: CancellationToken | None) -> None:
    if cancellation is not None:
        cancellation.checkpoint()


def _progress(
    callback: ProgressCallback | None, stage: str, percent: int, details: dict[str, Any],
) -> None:
    if callback is not None:
        callback(stage, percent, details)


def _qlib_lgb_params(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "loss": "mse",
        "learning_rate": float(source.get("learning_rate", 0.05)),
        "num_leaves": int(source.get("num_leaves", 31)),
        "max_depth": int(source.get("max_depth", -1)),
        "num_boost_round": int(source.get("n_estimators", 1000)),
        "early_stopping_rounds": int(source.get("early_stopping_rounds", 50)),
        "bagging_fraction": float(source.get("subsample", 0.9)),
        "feature_fraction": float(source.get("colsample_bytree", 0.9)),
        "lambda_l1": float(source.get("reg_alpha", 0.0)),
        "lambda_l2": float(source.get("reg_lambda", 0.0)),
        "min_child_samples": int(source.get("min_child_samples", 20)),
        "num_threads": int(source.get("num_threads", 4)),
        "seed": 42,
        "feature_fraction_seed": 42,
        "bagging_seed": 42,
        "data_random_seed": 42,
        "deterministic": True,
        "force_col_wise": True,
        "verbosity": -1,
    }


def _create_model(kind: str, source: dict[str, Any], feature_count: int) -> tuple[Any, dict[str, Any]]:
    if kind == "lightgbm":
        from qlib.contrib.model.gbdt import LGBModel

        params = _qlib_lgb_params(source)
        return LGBModel(**params), params
    if kind == "xgboost":
        try:
            from qlib.contrib.model.xgboost import XGBModel
        except ImportError as exc:
            raise RuntimeError("XGBoost尚未安装，请执行uv sync") from exc
        params = {
            "objective": "reg:squarederror",
            "eval_metric": "rmse",
            "eta": float(source.get("learning_rate", 0.05)),
            "max_depth": int(source.get("max_depth", 6)),
            "subsample": float(source.get("subsample", 0.9)),
            "colsample_bytree": float(source.get("colsample_bytree", 0.9)),
            "alpha": float(source.get("reg_alpha", 0.0)),
            "lambda": float(source.get("reg_lambda", 1.0)),
            "min_child_weight": float(source.get("min_child_weight", 1.0)),
            "nthread": int(source.get("num_threads", 4)),
            "seed": int(source.get("seed", 42)),
            "verbosity": 0,
        }
        model = XGBModel(**params)
        model._alphablocks_num_boost_round = int(source.get("n_estimators", 1000))
        model._alphablocks_early_stopping_rounds = int(source.get("early_stopping_rounds", 50))
        return model, {**params, "num_boost_round": model._alphablocks_num_boost_round,
                       "early_stopping_rounds": model._alphablocks_early_stopping_rounds}
    if kind == "catboost":
        try:
            from qlib.contrib.model.catboost_model import CatBoostModel
        except ImportError as exc:
            raise RuntimeError("CatBoost尚未安装，请执行uv sync") from exc
        params = {
            "learning_rate": float(source.get("learning_rate", 0.05)),
            "depth": int(source.get("depth", 6)),
            "l2_leaf_reg": float(source.get("l2_leaf_reg", 3.0)),
            "random_strength": float(source.get("random_strength", 1.0)),
            "thread_count": int(source.get("num_threads", 4)),
            "random_seed": int(source.get("seed", 42)),
            "allow_writing_files": False,
        }
        model = CatBoostModel(loss="RMSE", **params)
        model._alphablocks_num_boost_round = int(source.get("n_estimators", 1000))
        model._alphablocks_early_stopping_rounds = int(source.get("early_stopping_rounds", 50))
        return model, {**params, "num_boost_round": model._alphablocks_num_boost_round,
                       "early_stopping_rounds": model._alphablocks_early_stopping_rounds}
    if kind == "mlp":
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("PyTorch尚未安装，请执行uv sync") from exc
        from factor_service.research.models import QlibTorchMLPModel

        hidden_size = int(source.get("hidden_size", 64))
        layer_count = int(source.get("layer_count", 2))
        params = {
            "learning_rate": float(source.get("learning_rate", 0.001)),
            "max_steps": int(source.get("max_steps", 300)),
            "batch_size": int(source.get("batch_size", 2048)),
            "early_stopping_rounds": int(source.get("early_stopping_rounds", 10)),
            "eval_steps": int(source.get("eval_steps", 10)),
            "seed": int(source.get("seed", 42)),
            "weight_decay": float(source.get("weight_decay", 0.0001)),
            "input_dim": feature_count,
            "hidden_size": hidden_size,
            "layer_count": layer_count,
            "num_threads": int(source.get("num_threads", 4)),
        }
        return QlibTorchMLPModel(**params), params
    raise ValueError(f"不支持的模型: {kind}")


def predict_feature_frame(model: Any, model_kind: str, features: pd.DataFrame) -> np.ndarray:
    """Predict an already preprocessed feature frame from a serialized Qlib model."""
    if model_kind == "xgboost":
        import xgboost as xgb

        return np.asarray(model.model.predict(xgb.DMatrix(features.values)), dtype=float)
    if model_kind == "mlp":
        return np.asarray(model.predict_frame(features), dtype=float).reshape(-1)
    predictor = getattr(model, "model", None)
    if predictor is None or not hasattr(predictor, "predict"):
        raise ValueError(f"{model_kind}模型产物不包含可用预测器")
    values = features.values if model_kind in {"lightgbm", "catboost"} else features
    return np.asarray(predictor.predict(values), dtype=float).reshape(-1)


def _model_package_version(kind: str) -> dict[str, str]:
    package = {"lightgbm": "lightgbm", "xgboost": "xgboost", "catboost": "catboost", "mlp": "torch"}[kind]
    module = __import__(package)
    return {package: str(getattr(module, "__version__", "unknown"))}


def _prediction_frame(prediction: pd.Series, prepared: PreparedDataset, job: dict[str, Any]) -> pd.DataFrame:
    frame = prediction.rename("raw_prediction").reset_index()
    frame.rename(columns={"datetime": "trade_date", "instrument": "entity_code"}, inplace=True)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    grouped = frame.groupby("trade_date")["raw_prediction"]
    # rank_value=1始终代表当日预测最高的股票，便于页面和TopN语义一致。
    frame["rank_value"] = grouped.rank(method="first", ascending=False).astype(int)
    frame["percentile"] = grouped.rank(method="average", pct=True)
    frame["score"] = (2.0 * frame["percentile"] - 1.0).clip(-1.0, 1.0)
    trade_dates = pd.to_datetime(frame["trade_date"])
    if trade_dates.dt.tz is None:
        signal_dates = trade_dates.dt.tz_localize("Asia/Shanghai")
    else:
        signal_dates = trade_dates.dt.tz_convert("Asia/Shanghai")
    # ClickHouse DateTime('Asia/Shanghai') must receive timezone-aware values.
    # Passing naive pandas timestamps makes clickhouse-connect interpret them as
    # UTC, shifting both PIT audit fields forward by eight hours.
    frame["feature_cutoff_at"] = signal_dates + pd.Timedelta(hours=15)
    computed = pd.Timestamp.now(tz="Asia/Shanghai")
    frame["computed_at"] = computed
    frame["source_vintage"] = f"qlib#{job['job_id']}@{computed.isoformat()}"
    return frame


def _metrics(prediction: pd.Series, qlib_frame: pd.DataFrame, test_segment: tuple[str, str]) -> dict[str, float | int]:
    label = qlib_frame[("label", "LABEL0")]
    start, end = test_segment
    label = label.loc[pd.IndexSlice[start:end, :]]
    aligned = pd.concat([prediction.rename("prediction"), label.rename("label")], axis=1).dropna()
    daily_ic = aligned.groupby(level="datetime").apply(
        lambda group: group["prediction"].corr(group["label"], method="spearman")
        if group["prediction"].nunique() > 1 and group["label"].nunique() > 1 else np.nan,
        include_groups=False,
    ).dropna()
    rmse = float(np.sqrt(np.mean(np.square(aligned["prediction"] - aligned["label"]))))
    ic_mean = float(daily_ic.mean()) if not daily_ic.empty else 0.0
    ic_std = float(daily_ic.std(ddof=1)) if len(daily_ic) > 1 else 0.0
    return {
        "test_rows": int(len(aligned)),
        "test_days": int(aligned.index.get_level_values("datetime").nunique()),
        "rmse": rmse,
        "ic": ic_mean,
        "rank_ic": ic_mean,
        "ic_ir": ic_mean / ic_std if ic_std else 0.0,
    }


def _feature_importance(model: Any, feature_names: list[str]) -> list[dict[str, float | str | int]]:
    if hasattr(model, "get_feature_importance"):
        raw_importance = model.get_feature_importance()
        if isinstance(raw_importance, pd.Series):
            mapped = {str(key): float(value) for key, value in raw_importance.items()}
            values = [
                mapped.get(name, mapped.get(f"f{index}", 0.0))
                for index, name in enumerate(feature_names)
            ]
        else:
            values = list(raw_importance)
    elif hasattr(model, "dnn_model"):
        first_linear = next(
            (layer for layer in model.dnn_model.modules() if layer.__class__.__name__ == "Linear"), None,
        )
        if first_linear is None:
            values = [0.0] * len(feature_names)
        else:
            values = first_linear.weight.detach().abs().mean(dim=0).cpu().numpy().tolist()
    else:
        values = [0.0] * len(feature_names)
    rows = [
        {"factor": name, "importance": float(values[index]) if index < len(values) else 0.0}
        for index, name in enumerate(feature_names)
    ]
    rows.sort(key=lambda item: (-float(item["importance"]), str(item["factor"])))
    for index, item in enumerate(rows, start=1):
        item["rank"] = index
    return rows


__all__ = ["QlibTrainer", "TrainingResult", "_qlib_lgb_params", "predict_feature_frame"]
