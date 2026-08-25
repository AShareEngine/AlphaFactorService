from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import pickle
import tarfile
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from factor_service.research.control import ResearchControl
from factor_service.research.config import Settings
from factor_service.research.dataset import DatasetBuilder, _feature_name
from factor_service.research.errors import PermanentJobError
from factor_service.research.job import CancellationToken, ProgressCallback
from factor_service.research.trainer import (
    QlibStackingModel,
    SEQUENCE_MODEL_KINDS,
    TrainingResult,
    predict_feature_frame,
)


class DailyInferenceRunner:
    """Load an immutable training bundle and score one historical trading day."""

    def __init__(self, settings: Settings, control: ResearchControl) -> None:
        self.settings = settings
        self.control = control
        self.dataset_builder = DatasetBuilder(settings)

    def run(
        self,
        job: dict[str, Any],
        work_dir: Path,
        *,
        cancellation: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
    ) -> TrainingResult:
        config = dict(job["config_json"])
        source = dict(config["source_model"])
        inference = dict(config["inference"])
        trade_date = str(inference["trade_date"])
        data_cutoff = datetime.fromisoformat(str(inference["data_cutoff"]).replace("Z", "+00:00"))
        cutoff_for_clickhouse = data_cutoff.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        work_dir.mkdir(parents=True, exist_ok=True)
        _checkpoint(cancellation)
        _progress(progress, "downloading_model", 10, {"artifact_id": source["artifact_id"]})
        bundle_path = self.control.download_artifact(
            str(source["artifact_id"]),
            work_dir / "source_model.tar.gz",
            str(source["artifact_sha256"]),
        )
        _checkpoint(cancellation)
        model, training_manifest = _load_bundle(bundle_path)
        model_kind = str(training_manifest.get("model_kind") or "lightgbm")
        dataset_spec = dict(job.get("dataset_spec") or config.get("dataset") or {})
        research_target = str(
            dataset_spec.get("research_target") or "stock_selection"
        ).strip().lower()
        prediction_scope = str(
            dataset_spec.get("prediction_scope")
            or ("market_style" if research_target == "market_style"
                else "industry" if research_target == "industry_rotation"
                else "stock")
        ).strip().lower()
        if research_target not in {
            "stock_selection", "market_style", "industry_rotation",
        }:
            raise PermanentJobError(f"训练目标{research_target}尚不支持每日推理")
        expected_names = list(training_manifest.get("feature_names") or [])
        medians = dict(training_manifest.get("medians") or {})
        factors = list(job["dataset_spec"]["factors"])
        actual_names = [_feature_name(item) for item in factors]
        if actual_names != expected_names or any(name not in medians for name in expected_names):
            raise PermanentJobError("模型产物中的特征顺序或训练中位数与冻结因子不一致")

        lookback_window = 1
        feature_date_start = trade_date
        universe_id = str(dataset_spec.get("universe_id") or "csi500")
        index_code = str(dataset_spec.get("index_code") or "000905.SH")
        sample_filters = dataset_spec.get("sample_filters")
        universe_label = {
            "csi300": "沪深300", "csi500": "中证500", "csi800": "中证800",
            "csi1000": "中证1000", "all_a": "全A",
        }.get(universe_id, universe_id)
        if model_kind in SEQUENCE_MODEL_KINDS or model_kind == "stacking":
            model_params = dict(training_manifest.get("model_params") or {})
            if model_kind == "stacking":
                lookback_window = max([
                    int(dict(item.get("params") or {}).get("lookback_window") or 1)
                    for item in model_params.get("base_models") or []
                    if str(item.get("kind") or "") in SEQUENCE_MODEL_KINDS
                ] or [1])
            else:
                lookback_window = int(model_params.get("lookback_window") or 60)
        if lookback_window > 1:
            try:
                sequence_dates = self.dataset_builder.trading_dates_ending_at(
                    trade_date, lookback_window,
                    index_code=index_code, universe_id=universe_id,
                )
            except ValueError as exc:
                raise PermanentJobError(str(exc)) from exc
            feature_date_start = sequence_dates[0]

        _progress(progress, "building_inference_features", 35, {"trade_date": trade_date})
        target_membership = self.dataset_builder._membership(
            trade_date, trade_date, universe_id=universe_id, index_code=index_code,
            sample_filters=sample_filters,
        )
        if target_membership.empty:
            raise PermanentJobError(f"{trade_date}不是{universe_label}可推理交易日")
        membership = self.dataset_builder._membership(
            feature_date_start, trade_date,
            universe_id=universe_id, index_code=index_code,
            sample_filters=sample_filters,
        )
        features = membership[["trade_date", "instrument"]].drop_duplicates()
        coverages: dict[str, float] = {}
        expected_count = max(1, len(features))
        for index, (factor, feature_name) in enumerate(zip(factors, expected_names), start=1):
            _checkpoint(cancellation)
            _progress(progress, "loading_inference_factors", 35 + int(25 * (index - 1) / len(factors)), {
                "factor_id": factor["factor_id"],
                "factor_index": index,
                "factor_count": len(factors),
            })
            values = self.dataset_builder._factor_values(
                factor, cutoff_for_clickhouse, feature_date_start, trade_date,
            ).rename(columns={"value": feature_name})
            eligible_values = values.merge(
                features[["trade_date", "instrument"]],
                on=["trade_date", "instrument"], how="inner",
            )
            coverages[str(factor["factor_id"])] = (
                eligible_values[["trade_date", "instrument"]].drop_duplicates().shape[0] / expected_count
            )
            features = features.merge(
                eligible_values[["trade_date", "instrument", feature_name]],
                on=["trade_date", "instrument"], how="left",
            )
        minimum = float(job["dataset_spec"].get("minimum_factor_coverage") or 0.8)
        low = [name for name, coverage in coverages.items() if coverage < minimum]
        if low:
            raise PermanentJobError("每日推理因子覆盖率低于阈值: " + ", ".join(low))
        if research_target == "market_style":
            try:
                features = self.dataset_builder.market_style_features(
                    features, expected_names, feature_date_start, trade_date,
                )
            except ValueError as exc:
                raise PermanentJobError(str(exc)) from exc
        elif research_target == "industry_rotation":
            try:
                features = self.dataset_builder.industry_features(
                    features, expected_names, feature_date_start, trade_date,
                )
            except ValueError as exc:
                raise PermanentJobError(str(exc)) from exc
        features[expected_names] = features[expected_names].fillna({
            name: float(medians[name]) for name in expected_names
        })
        if features[expected_names].isna().any().any():
            raise PermanentJobError("每日推理特征填充后仍有缺失值")

        _checkpoint(cancellation)
        _progress(progress, "inferencing", 68, {"row_count": len(features)})
        sequence_coverage: float | None = None
        try:
            if model_kind == "stacking":
                if not isinstance(model, QlibStackingModel):
                    raise ValueError("Stacking模型产物类型无效")
                predictions = _predict_stacking(
                    model,
                    features=features,
                    feature_names=expected_names,
                    trade_date=trade_date,
                )
                raw = predictions["raw_prediction"].to_numpy(dtype=float)
                target_count = (
                    2 if research_target == "market_style"
                    else int(features.loc[
                        features["trade_date"] == pd.Timestamp(trade_date),
                        "instrument",
                    ].nunique()) if research_target == "industry_rotation"
                    else max(1, target_membership["instrument"].nunique())
                )
                sequence_coverage = len(predictions) / target_count
                if sequence_coverage < minimum:
                    raise ValueError(
                        f"Stacking共同预测覆盖率{sequence_coverage:.2%}低于阈值"
                    )
            elif model_kind in SEQUENCE_MODEL_KINDS:
                from qlib.data.dataset import DataHandlerLP, TSDatasetH

                sequence_frame = features.set_index(["trade_date", "instrument"])[expected_names]
                sequence_frame.index.names = ["datetime", "instrument"]
                sequence_frame.columns = pd.MultiIndex.from_tuples(
                    [("feature", name) for name in expected_names]
                )
                inference_dataset = TSDatasetH(
                    handler=DataHandlerLP.from_df(sequence_frame),
                    segments={"infer": (trade_date, trade_date)},
                    step_len=lookback_window,
                )
                sequence_prediction = model.predict(inference_dataset, segment="infer")
                predictions = sequence_prediction.rename("raw_prediction").reset_index()
                predictions.rename(
                    columns={"datetime": "trade_date", "instrument": "entity_code"},
                    inplace=True,
                )
                target_count = (
                    2 if research_target == "market_style"
                    else int(features.loc[
                        features["trade_date"] == pd.Timestamp(trade_date),
                        "instrument",
                    ].nunique()) if research_target == "industry_rotation"
                    else max(1, target_membership["instrument"].nunique())
                )
                sequence_coverage = len(predictions) / target_count
                if sequence_coverage < minimum:
                    raise ValueError(f"时序模型完整历史窗口覆盖率{sequence_coverage:.2%}低于阈值")
                raw = predictions["raw_prediction"].to_numpy(dtype=float)
            else:
                raw = predict_feature_frame(model, model_kind, features[expected_names])
                predictions = features[["trade_date", "instrument"]].rename(
                    columns={"instrument": "entity_code"},
                )
                predictions["raw_prediction"] = raw
        except (ImportError, RuntimeError, ValueError) as exc:
            raise PermanentJobError(f"{model_kind}模型推理失败: {exc}") from exc
        expected_prediction_rows = len(predictions)
        if raw.shape[0] != expected_prediction_rows or not np.isfinite(raw).all():
            raise PermanentJobError("模型推理结果数量不一致或包含非有限值")
        predictions["trade_date"] = pd.to_datetime(predictions["trade_date"])
        grouped = predictions.groupby("trade_date")["raw_prediction"]
        predictions["rank_value"] = grouped.rank(method="first", ascending=False).astype(int)
        predictions["percentile"] = grouped.rank(method="average", pct=True)
        if prediction_scope in {"market_style", "industry"}:
            counts = grouped.transform("size")
            predictions["score"] = np.where(
                counts > 1,
                1.0 - 2.0 * (predictions["rank_value"] - 1.0) / (counts - 1.0),
                0.0,
            )
        else:
            predictions["score"] = (
                2.0 * predictions["percentile"] - 1.0
            ).clip(-1.0, 1.0)
        predictions["feature_cutoff_at"] = pd.Timestamp(inference["feature_cutoff_at"])
        computed_at = pd.Timestamp.now(tz="Asia/Shanghai")
        predictions["computed_at"] = computed_at
        predictions["source_vintage"] = f"qlib-daily#{job['job_id']}@{computed_at.isoformat()}"
        predictions_path = work_dir / "predictions.parquet"
        predictions.to_parquet(predictions_path, index=False)
        future_function_guards = [
            "frozen factor definitions computed on demand from source data",
            "source rows limited to signal date and available by market close",
            "inference data_cutoff >= signal date close",
            "historical index membership",
            "causal per-instrument history ending at signal date",
            "training-fitted medians only",
        ]
        if research_target == "industry_rotation":
            future_function_guards.append(
                "exact-date SW2021 industry snapshots no earlier than 2021-12-13"
            )
        manifest = {
            "schema_version": "alphablocks.qlib-inference.v1",
            "job_id": job["job_id"],
            "model_id": job["model_id"],
            "model_version": int(config["planned_model_version"]),
            "model_kind": model_kind,
            "research_target": research_target,
            "prediction_scope": prediction_scope,
            "dataset_hash": job["dataset_hash"],
            "training_job_id": source["training_job_id"],
            "trade_date": trade_date,
            "feature_date_start": feature_date_start,
            "lookback_window": lookback_window,
            "data_cutoff": inference["data_cutoff"],
            "feature_cutoff_at": inference["feature_cutoff_at"],
            "feature_names": expected_names,
            "coverage": coverages,
            "sequence_coverage": sequence_coverage,
            "medians_source": "training_manifest",
            "row_count": len(predictions),
            "future_function_guards": future_function_guards,
            "created_at": computed_at.isoformat(),
        }
        manifest_path = work_dir / "inference_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8",
        )
        result = {
            "metrics": {},
            "feature_importance": [],
            "predictions": {
                "row_count": len(predictions),
                "date_start": trade_date,
                "date_end": trade_date,
                "inference_run_id": str(job["job_id"]),
                "model_version": int(config["planned_model_version"]),
            },
            "manifest": manifest,
        }
        _progress(progress, "packaged", 88, {"prediction_rows": len(predictions)})
        return TrainingResult(
            result=result,
            artifacts=[("inference_predictions", predictions_path), ("inference_manifest", manifest_path)],
            predictions_path=predictions_path,
        )


def _predict_stacking(
    model: QlibStackingModel,
    *,
    features: pd.DataFrame,
    feature_names: list[str],
    trade_date: str,
) -> pd.DataFrame:
    from qlib.data.dataset import DataHandlerLP, TSDatasetH

    target_date = pd.Timestamp(trade_date)
    date_values = pd.to_datetime(features["trade_date"])
    target = features.loc[date_values == target_date].copy()
    if target.empty:
        raise ValueError(f"{trade_date}没有可用于Stacking推理的目标截面")
    target_index = pd.MultiIndex.from_arrays(
        [
            pd.to_datetime(target["trade_date"]),
            target["instrument"].astype(str),
        ],
        names=["datetime", "instrument"],
    )
    base_predictions: dict[str, pd.Series] = {}
    sequence_frame: pd.DataFrame | None = None
    for item in model.base_models:
        kind = str(item.get("kind") or "").strip().lower()
        base_model = item.get("model")
        params = dict(item.get("params") or {})
        if kind in SEQUENCE_MODEL_KINDS:
            if sequence_frame is None:
                sequence_frame = features.set_index(
                    ["trade_date", "instrument"],
                )[feature_names]
                sequence_frame.index.names = ["datetime", "instrument"]
                sequence_frame.columns = pd.MultiIndex.from_tuples(
                    [("feature", name) for name in feature_names]
                )
            dataset = TSDatasetH(
                handler=DataHandlerLP.from_df(sequence_frame),
                segments={"infer": (trade_date, trade_date)},
                step_len=int(params.get("lookback_window") or 60),
            )
            base_predictions[kind] = base_model.predict(
                dataset, segment="infer",
            ).rename(kind)
        else:
            values = predict_feature_frame(
                base_model, kind, target[feature_names],
            )
            base_predictions[kind] = pd.Series(
                np.asarray(values, dtype=float).reshape(-1),
                index=target_index,
                name=kind,
            )
    aligned = pd.concat(base_predictions, axis=1, join="inner").dropna()
    if aligned.empty:
        raise ValueError("Stacking基模型没有共同有效的当日预测")
    raw = model.combine([
        aligned[item["kind"]].to_numpy(dtype=float) for item in model.base_models
    ])
    result = pd.Series(raw, index=aligned.index, name="raw_prediction").reset_index()
    result.rename(columns={"instrument": "entity_code"}, inplace=True)
    return result


def _load_bundle(path: Path) -> tuple[Any, dict[str, Any]]:
    payloads: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as archive:
        for required in ("model.pkl", "manifest.json"):
            member = archive.getmember(required)
            if not member.isfile() or member.size <= 0 or member.size > 256 * 1024 * 1024:
                raise PermanentJobError(f"模型产物中的{required}无效")
            source = archive.extractfile(member)
            if source is None:
                raise PermanentJobError(f"无法读取模型产物中的{required}")
            payloads[required] = source.read()
    try:
        model = pickle.loads(payloads["model.pkl"])
        manifest = json.loads(payloads["manifest.json"].decode("utf-8"))
    except Exception as exc:
        raise PermanentJobError(f"模型产物解析失败: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PermanentJobError("训练manifest必须是JSON对象")
    return model, manifest


def _checkpoint(cancellation: CancellationToken | None) -> None:
    if cancellation is not None:
        cancellation.checkpoint()


def _progress(
    callback: ProgressCallback | None, stage: str, percent: int, details: dict[str, Any],
) -> None:
    if callback is not None:
        callback(stage, percent, details)


__all__ = ["DailyInferenceRunner"]
