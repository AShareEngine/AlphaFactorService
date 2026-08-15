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

from factor_service.model_validation import assess_walk_forward_stability
from factor_service.research.config import Settings
from factor_service.research.dataset import (
    DatasetBuilder,
    PreparedDataset,
    walk_forward_segments,
)
from factor_service.research.job import CancellationToken, ProgressCallback
from factor_service.research.snapshot import DatasetSnapshotStore
from factor_service.research.training_diagnostics import build_training_diagnostics


@dataclass(frozen=True)
class TrainingResult:
    result: dict[str, Any]
    artifacts: list[tuple[str, Path]]
    predictions_path: Path


class QlibTrainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.dataset_builder = DatasetBuilder(settings)
        self.snapshot_store = DatasetSnapshotStore(settings.model_artifacts_root)

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
        snapshot = self.snapshot_store.get_or_create(
            job, work_dir, self.dataset_builder,
            cancellation=cancellation, progress=progress,
        )
        prepared = snapshot.prepared
        config = dict(job.get("config_json") or {})
        walk_forward_config = dict(config.get("walk_forward") or {})
        dataset_path = snapshot.dataset_path
        raw_dataset_path = snapshot.raw_dataset_path
        dataset_manifest_path = snapshot.manifest_path
        raw_params = dict(config.get("model", {}).get("params") or {})
        model_kind = str(config.get("model", {}).get("kind") or "lightgbm")
        handler = DataHandlerLP.from_df(prepared.frame)
        dataset = _dataset_for_model(
            handler, prepared.segments, model_kind, raw_params, DatasetH,
        )
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
        _prepare_recorder_experiment(recorder_uri, experiment_name, recorder_root)
        evals_result: dict[str, Any] = {}
        walk_forward_report: dict[str, Any] | None = None
        walk_forward_prediction: pd.Series | None = None
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
            if walk_forward_config.get("enabled") is True:
                walk_forward_prediction, walk_forward_report = _run_walk_forward(
                    prepared,
                    walk_forward_config,
                    model_kind=model_kind,
                    raw_params=raw_params,
                    DataHandlerLP=DataHandlerLP,
                    DatasetH=DatasetH,
                    cancellation=cancellation,
                    progress=progress,
                )
                training_start = 73
            else:
                training_start = 58
            _progress(progress, "training_final_model", training_start, {"iteration": 0})
            _fit_model(
                model_kind, model, dataset, evals_result,
                cancellation=cancellation, progress=progress,
                stage="training_final_model",
                progress_start=training_start,
                progress_end=80,
                metric_prefix="final.",
            )
            _checkpoint(cancellation)
            _progress(progress, "predicting", 82, {})
            valid_prediction = model.predict(dataset, segment="valid")
            test_prediction = model.predict(dataset, segment="test")
            R.save_objects(trained_model=model)
            recorder_id = R.get_recorder().id
        # 研究回测只允许使用完全样本外的test段；train/valid预测不得进入模型信号库。
        # 开启Walk-Forward时发布各窗口拼接后的OOS预测，正式模型仍单独保存用于后续每日推理。
        published_prediction = walk_forward_prediction if walk_forward_prediction is not None else test_prediction
        prediction_frame = _prediction_frame(published_prediction, prepared, job)
        predictions_path = work_dir / "predictions.parquet"
        prediction_frame.to_parquet(predictions_path, index=False)
        prediction_rows = len(prediction_frame)
        standard_metrics = _metrics(test_prediction, prepared.frame, prepared.segments["test"])
        raw_validation_metrics = _metrics(
            valid_prediction, prepared.frame, prepared.segments["valid"],
        )
        validation_metrics = {
            "rows": raw_validation_metrics["test_rows"],
            "days": raw_validation_metrics["test_days"],
            "rmse": raw_validation_metrics["rmse"],
            "ic": raw_validation_metrics["ic"],
            "rank_ic": raw_validation_metrics["rank_ic"],
            "ic_ir": raw_validation_metrics["ic_ir"],
        }
        if walk_forward_report is not None:
            metrics: dict[str, Any] = {
                **walk_forward_report["aggregate"],
                "evaluation_mode": "walk_forward",
                "walk_forward_windows": walk_forward_report["window_count"],
                "standard_test": standard_metrics,
                "validation": validation_metrics,
            }
        else:
            metrics = {
                **standard_metrics,
                "evaluation_mode": "single_split",
                "validation": validation_metrics,
            }
        feature_importance = _feature_importance(model, prepared.feature_names)
        training_diagnostics = build_training_diagnostics(
            model_kind, evals_result, model_params,
        )
        dataset_spec = dict(job.get("dataset_spec") or config.get("dataset") or {})
        research_target = str(
            dataset_spec.get("research_target") or "stock_selection"
        )
        prediction_scope = str(
            dataset_spec.get("prediction_scope") or "stock"
        )
        manifest = {
            **prepared.manifest,
            "schema_version": "alphablocks.qlib-training.v1",
            "job_id": job["job_id"],
            "model_id": job["model_id"],
            "model_kind": model_kind,
            "research_target": research_target,
            "prediction_scope": prediction_scope,
            "model_version": int((job.get("config_json") or {}).get("planned_model_version") or 1),
            "qlib_recorder_id": recorder_id,
            "qlib_recorder_uri": recorder_uri,
            "model_params": model_params,
            "validation_metrics": validation_metrics,
            "training_diagnostics": training_diagnostics,
            "walk_forward": walk_forward_report,
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
        training_diagnostics_path = work_dir / "training_diagnostics.json"
        config_path = work_dir / "task_config.json"
        model_path = work_dir / "model.pkl"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        importance_path.write_text(json.dumps(feature_importance, ensure_ascii=False, indent=2), encoding="utf-8")
        training_diagnostics_path.write_text(
            json.dumps(training_diagnostics, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        config_path.write_text(json.dumps(job.get("config_json") or {}, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        with model_path.open("wb") as target:
            pickle.dump(model, target)
        bundle_path = work_dir / "qlib_experiment.tar.gz"
        with tarfile.open(bundle_path, "w:gz") as archive:
            for path in [manifest_path, metrics_path, importance_path, training_diagnostics_path, config_path, model_path, predictions_path, dataset_manifest_path]:
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
            ("dataset_raw", raw_dataset_path),
            ("dataset_manifest", dataset_manifest_path),
            ("predictions", predictions_path),
            ("manifest", manifest_path),
            ("training_diagnostics", training_diagnostics_path),
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
            raise ValueError("预测文件包含重复的日期与预测实体")
        dataset_spec = dict(
            job.get("dataset_spec")
            or (job.get("config_json") or {}).get("dataset")
            or {}
        )
        prediction_scope = str(
            dataset_spec.get("prediction_scope") or "stock"
        ).strip().lower()
        if prediction_scope not in {"stock", "market_style", "industry"}:
            raise ValueError(f"不支持的预测实体类型: {prediction_scope}")
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
                row.trade_date, prediction_scope, row.entity_code, job["model_id"],
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


def _prepare_recorder_experiment(
    recorder_uri: str,
    experiment_name: str,
    recorder_root: Path,
) -> None:
    """Keep Qlib Recorder artifacts inside the current job directory."""
    from mlflow.tracking import MlflowClient

    recorder_root.mkdir(parents=True, exist_ok=True)
    client = MlflowClient(tracking_uri=recorder_uri)
    if client.get_experiment_by_name(experiment_name) is None:
        client.create_experiment(
            experiment_name,
            artifact_location=recorder_root.resolve().as_uri(),
        )


def _run_walk_forward(
    prepared: PreparedDataset,
    config: dict[str, Any],
    *,
    model_kind: str,
    raw_params: dict[str, Any],
    DataHandlerLP: Any,
    DatasetH: Any,
    cancellation: CancellationToken | None,
    progress: ProgressCallback | None,
) -> tuple[pd.Series, dict[str, Any]]:
    raw_frame = prepared.raw_frame
    if raw_frame is None:
        raise ValueError("Walk-Forward缺少未填充的冻结数据集")
    dates = pd.Index(raw_frame.index.get_level_values("datetime").unique())
    windows = walk_forward_segments(
        dates,
        strategy=str(config.get("strategy") or "rolling"),
        train_years=int(config.get("train_years") or 3),
        valid_months=int(config.get("valid_months") or 6),
        test_months=int(config.get("test_months") or 12),
        step_months=int(config.get("step_months") or 12),
        max_windows=int(config.get("max_windows") or 4),
        embargo_days=int(config.get("embargo_days") or 5),
    )
    if not windows:
        raise ValueError("没有可执行的Walk-Forward窗口")

    predictions: list[pd.Series] = []
    reports: list[dict[str, Any]] = []
    total = len(windows)
    for index, segments in enumerate(windows, start=1):
        _checkpoint(cancellation)
        start_percent = 58 + int(14 * (index - 1) / total)
        end_percent = 58 + int(14 * index / total)
        details = {"window_index": index, "window_count": total, "segments": segments}
        _progress(progress, "walk_forward_training", start_percent, details)
        window_frame, medians = _walk_forward_frame(prepared, segments)
        dataset = _dataset_for_model(
            DataHandlerLP.from_df(window_frame), segments, model_kind, raw_params, DatasetH,
        )
        model, _ = _create_model(model_kind, raw_params, len(prepared.feature_names))
        evals_result: dict[str, Any] = {}
        _fit_model(
            model_kind,
            model,
            dataset,
            evals_result,
            cancellation=cancellation,
            progress=progress,
            stage="walk_forward_training",
            progress_start=start_percent,
            progress_end=end_percent,
            progress_details=details,
            metric_prefix=f"walk_forward.window_{index}.",
        )
        prediction = model.predict(dataset, segment="test")
        window_metrics = _metrics(prediction, raw_frame, segments["test"])
        predictions.append(prediction)
        reports.append({
            "window": index,
            "segments": segments,
            "metrics": window_metrics,
            "train_medians": medians,
        })

    stitched = pd.concat(predictions).sort_index()
    if stitched.index.duplicated().any():
        raise ValueError("Walk-Forward样本外预测日期重叠")
    aggregate = _metrics(
        stitched,
        raw_frame,
        (windows[0]["test"][0], windows[-1]["test"][1]),
    )
    window_ics = np.asarray([float(item["metrics"]["ic"]) for item in reports], dtype=float)
    aggregate.update({
        "window_ic_mean": float(window_ics.mean()),
        "window_ic_std": float(window_ics.std(ddof=1)) if len(window_ics) > 1 else 0.0,
        "positive_ic_window_ratio": float(np.mean(window_ics > 0)),
    })
    report = {
        "schema_version": "alphablocks.walk-forward.v1",
        "enabled": True,
        "strategy": str(config.get("strategy") or "rolling"),
        "train_years": int(config.get("train_years") or 3),
        "valid_months": int(config.get("valid_months") or 6),
        "test_months": int(config.get("test_months") or 12),
        "step_months": int(config.get("step_months") or 12),
        "embargo_days": int(config.get("embargo_days") or 5),
        "window_count": total,
        "windows": reports,
        "aggregate": aggregate,
        "prediction_date_start": windows[0]["test"][0],
        "prediction_date_end": windows[-1]["test"][1],
        "stability": assess_walk_forward_stability(
            aggregate, window_count=total,
        ),
    }
    return stitched, report


def _walk_forward_frame(
    prepared: PreparedDataset,
    segments: dict[str, tuple[str, str]],
) -> tuple[pd.DataFrame, dict[str, float]]:
    raw_frame = prepared.raw_frame
    if raw_frame is None:
        raise ValueError("Walk-Forward缺少原始特征")
    train_start, train_end = segments["train"]
    train_features = raw_frame.loc[
        pd.IndexSlice[train_start:train_end, :], pd.IndexSlice["feature", :]
    ]
    medians = {
        name: float(pd.to_numeric(train_features[("feature", name)], errors="coerce").median())
        for name in prepared.feature_names
    }
    missing = [name for name, value in medians.items() if not np.isfinite(value)]
    if missing:
        raise ValueError("Walk-Forward训练段无法计算因子中位数: " + ", ".join(missing))
    window_start = segments["train"][0]
    window_end = segments["test"][1]
    frame = raw_frame.loc[pd.IndexSlice[window_start:window_end, :], :].copy()
    for name, value in medians.items():
        frame[("feature", name)] = frame[("feature", name)].fillna(value)
    return frame, medians


def _fit_model(
    model_kind: str,
    model: Any,
    dataset: Any,
    evals_result: dict[str, Any],
    *,
    cancellation: CancellationToken | None,
    progress: ProgressCallback | None,
    stage: str = "training",
    progress_start: int = 58,
    progress_end: int = 80,
    progress_details: dict[str, Any] | None = None,
    metric_prefix: str = "",
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
            if model_kind == "catboost" and not evals_result:
                raw_evaluations = model.model.get_evals_result()
                train_metrics = dict(raw_evaluations.get("learn") or {})
                valid_metrics = dict(raw_evaluations.get("validation") or {})
                shared_metrics = [name for name in train_metrics if name in valid_metrics]
                if shared_metrics:
                    metric = shared_metrics[0]
                    evals_result.update({
                        "train": list(train_metrics[metric]),
                        "valid": list(valid_metrics[metric]),
                    })
        else:
            def mapped_progress(
                _stage: str, percent: int, details: dict[str, Any],
            ) -> None:
                ratio = min(1.0, max(0.0, (int(percent) - 58) / 22))
                mapped = progress_start + int((progress_end - progress_start) * ratio)
                _progress(progress, stage, mapped, {**(progress_details or {}), **details})

            model.fit(
                dataset, evals_result=evals_result,
                cancellation=cancellation, progress=mapped_progress,
            )
        _checkpoint(cancellation)
        _progress(
            progress, stage, progress_end,
            {**(progress_details or {}), "model_kind": model_kind, "completed": True},
        )
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
            percent = min(
                progress_end,
                progress_start + int((progress_end - progress_start) * iteration / total),
            )
            _progress(
                progress,
                stage,
                percent,
                {
                    **(progress_details or {}),
                    "iteration": iteration,
                    "total_iterations": total,
                },
            )

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
                metric_name = f"{metric_prefix}{metric}.{name}".replace("@", "_")
                R.log_metrics(**{metric_name: value}, step=epoch)
    _progress(
        progress, stage, progress_end,
        {**(progress_details or {}), "model_kind": model_kind, "completed": True},
    )


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

        raw_layers = source.get("hidden_layers")
        if isinstance(raw_layers, list) and raw_layers:
            hidden_layers = [int(width) for width in raw_layers]
        else:
            hidden_layers = [int(source.get("hidden_size", 64))] * int(
                source.get("layer_count", 2)
            )
        params = {
            "learning_rate": float(source.get("learning_rate", 0.001)),
            "max_steps": int(source.get("max_steps", 300)),
            "batch_size": int(source.get("batch_size", 2048)),
            "early_stopping_rounds": int(source.get("early_stopping_rounds", 10)),
            "eval_steps": int(source.get("eval_steps", 10)),
            "seed": int(source.get("seed", 42)),
            "weight_decay": float(source.get("weight_decay", 0.0001)),
            "input_dim": feature_count,
            "hidden_layers": hidden_layers,
            "num_threads": int(source.get("num_threads", 4)),
        }
        return QlibTorchMLPModel(**params), params
    if kind == "lstm":
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("PyTorch尚未安装，请执行uv sync") from exc
        from factor_service.research.models import QlibTorchLSTMModel

        params = {
            "learning_rate": float(source.get("learning_rate", 0.001)),
            "lookback_window": int(source.get("lookback_window", 60)),
            "hidden_size": int(source.get("hidden_size", 128)),
            "num_layers": int(source.get("num_layers", 2)),
            "dropout": float(source.get("dropout", 0.2)),
            "max_steps": int(source.get("max_steps", 300)),
            "batch_size": int(source.get("batch_size", 512)),
            "early_stopping_rounds": int(source.get("early_stopping_rounds", 10)),
            "eval_steps": int(source.get("eval_steps", 10)),
            "seed": int(source.get("seed", 42)),
            "weight_decay": float(source.get("weight_decay", 0.0001)),
            "input_dim": feature_count,
            "num_threads": int(source.get("num_threads", 4)),
        }
        return QlibTorchLSTMModel(**params), params
    if kind == "transformer_lstm":
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("PyTorch尚未安装，请执行uv sync") from exc
        from factor_service.research.models import QlibTorchTransformerLSTMModel

        params = {
            "learning_rate": float(source.get("learning_rate", 0.001)),
            "lookback_window": int(source.get("lookback_window", 60)),
            "d_model": int(source.get("d_model", 64)),
            "nhead": int(source.get("nhead", 4)),
            "transformer_layers": int(source.get("transformer_layers", 2)),
            "dim_feedforward": int(source.get("dim_feedforward", 256)),
            "lstm_hidden_size": int(source.get("lstm_hidden_size", 128)),
            "lstm_layers": int(source.get("lstm_layers", 1)),
            "dropout": float(source.get("dropout", 0.2)),
            "max_steps": int(source.get("max_steps", 300)),
            "batch_size": int(source.get("batch_size", 256)),
            "early_stopping_rounds": int(source.get("early_stopping_rounds", 10)),
            "eval_steps": int(source.get("eval_steps", 10)),
            "seed": int(source.get("seed", 42)),
            "weight_decay": float(source.get("weight_decay", 0.0001)),
            "input_dim": feature_count,
            "num_threads": int(source.get("num_threads", 4)),
        }
        return QlibTorchTransformerLSTMModel(**params), params
    raise ValueError(f"不支持的模型: {kind}")


def _dataset_for_model(
    handler: Any,
    segments: dict[str, tuple[str, str]],
    model_kind: str,
    params: dict[str, Any],
    DatasetH: Any,
) -> Any:
    if model_kind not in {"lstm", "transformer_lstm"}:
        return DatasetH(handler=handler, segments=segments)
    from qlib.data.dataset import TSDatasetH

    return TSDatasetH(
        handler=handler,
        segments=segments,
        step_len=int(params.get("lookback_window", 60)),
    )


def predict_feature_frame(model: Any, model_kind: str, features: pd.DataFrame) -> np.ndarray:
    """Predict an already preprocessed feature frame from a serialized Qlib model."""
    if model_kind == "xgboost":
        import xgboost as xgb

        return np.asarray(model.model.predict(xgb.DMatrix(features.values)), dtype=float)
    if model_kind == "mlp":
        return np.asarray(model.predict_frame(features), dtype=float).reshape(-1)
    if model_kind in {"lstm", "transformer_lstm"}:
        raise ValueError("时序模型推理必须通过TSDatasetH提供按股票组织的历史窗口")
    predictor = getattr(model, "model", None)
    if predictor is None or not hasattr(predictor, "predict"):
        raise ValueError(f"{model_kind}模型产物不包含可用预测器")
    values = features.values if model_kind in {"lightgbm", "catboost"} else features
    return np.asarray(predictor.predict(values), dtype=float).reshape(-1)


def _model_package_version(kind: str) -> dict[str, str]:
    package = {
        "lightgbm": "lightgbm", "xgboost": "xgboost", "catboost": "catboost",
        "mlp": "torch", "lstm": "torch", "transformer_lstm": "torch",
    }[kind]
    module = __import__(package)
    return {package: str(getattr(module, "__version__", "unknown"))}


def _prediction_frame(prediction: pd.Series, prepared: PreparedDataset, job: dict[str, Any]) -> pd.DataFrame:
    frame = prediction.rename("raw_prediction").reset_index()
    frame.rename(columns={"datetime": "trade_date", "instrument": "entity_code"}, inplace=True)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    grouped = frame.groupby("trade_date")["raw_prediction"]
    # rank_value=1始终代表当日预测最高的实体，便于页面和TopN语义一致。
    frame["rank_value"] = grouped.rank(method="first", ascending=False).astype(int)
    frame["percentile"] = grouped.rank(method="average", pct=True)
    dataset_spec = dict(
        job.get("dataset_spec")
        or (job.get("config_json") or {}).get("dataset")
        or {}
    )
    prediction_scope = str(
        dataset_spec.get("prediction_scope") or "stock"
    ).strip().lower()
    if prediction_scope in {"market_style", "industry"}:
        # 风格与行业截面实体较少，使用完整区间映射，保证首尾为+1/-1，
        # 而不是普通pct rank在N个样本时只能覆盖(-1, 1]。
        counts = grouped.transform("size")
        frame["score"] = np.where(
            counts > 1,
            1.0 - 2.0 * (frame["rank_value"] - 1.0) / (counts - 1.0),
            0.0,
        )
    else:
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
                mapped.get(
                    name,
                    mapped.get(
                        f"Column_{index}",
                        mapped.get(f"f{index}", 0.0),
                    ),
                )
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
