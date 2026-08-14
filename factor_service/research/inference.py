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

from factor_service.research.api import AlphaBlocksApi
from factor_service.research.config import Settings
from factor_service.research.dataset import DatasetBuilder, _feature_name
from factor_service.research.errors import PermanentJobError
from factor_service.research.job import CancellationToken, ProgressCallback
from factor_service.research.trainer import TrainingResult, predict_feature_frame


class DailyInferenceRunner:
    """Load an immutable training bundle and score one historical trading day."""

    def __init__(self, settings: Settings, api: AlphaBlocksApi) -> None:
        self.settings = settings
        self.api = api
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
        bundle_path = self.api.download_artifact(
            str(source["artifact_id"]),
            work_dir / "source_model.tar.gz",
            str(source["artifact_sha256"]),
        )
        _checkpoint(cancellation)
        model, training_manifest = _load_bundle(bundle_path)
        model_kind = str(training_manifest.get("model_kind") or "lightgbm")
        expected_names = list(training_manifest.get("feature_names") or [])
        medians = dict(training_manifest.get("medians") or {})
        factors = list(job["dataset_spec"]["factors"])
        actual_names = [_feature_name(item) for item in factors]
        if actual_names != expected_names or any(name not in medians for name in expected_names):
            raise PermanentJobError("模型产物中的特征顺序或训练中位数与冻结因子不一致")

        _progress(progress, "building_inference_features", 35, {"trade_date": trade_date})
        membership = self.dataset_builder._membership(trade_date, trade_date)
        if membership.empty:
            raise PermanentJobError(f"{trade_date}不是中证500可推理交易日")
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
                factor, cutoff_for_clickhouse, trade_date, trade_date,
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
        features[expected_names] = features[expected_names].fillna({
            name: float(medians[name]) for name in expected_names
        })
        if features[expected_names].isna().any().any():
            raise PermanentJobError("每日推理特征填充后仍有缺失值")

        _checkpoint(cancellation)
        _progress(progress, "inferencing", 68, {"row_count": len(features)})
        try:
            raw = predict_feature_frame(model, model_kind, features[expected_names])
        except (ImportError, RuntimeError, ValueError) as exc:
            raise PermanentJobError(f"{model_kind}模型推理失败: {exc}") from exc
        if raw.shape[0] != len(features) or not np.isfinite(raw).all():
            raise PermanentJobError("模型推理结果数量不一致或包含非有限值")
        predictions = features[["trade_date", "instrument"]].rename(
            columns={"instrument": "entity_code"},
        )
        predictions["raw_prediction"] = raw
        grouped = predictions.groupby("trade_date")["raw_prediction"]
        predictions["rank_value"] = grouped.rank(method="first", ascending=False).astype(int)
        predictions["percentile"] = grouped.rank(method="average", pct=True)
        predictions["score"] = (2.0 * predictions["percentile"] - 1.0).clip(-1.0, 1.0)
        predictions["feature_cutoff_at"] = pd.Timestamp(inference["feature_cutoff_at"])
        computed_at = pd.Timestamp.now(tz="Asia/Shanghai")
        predictions["computed_at"] = computed_at
        predictions["source_vintage"] = f"qlib-daily#{job['job_id']}@{computed_at.isoformat()}"
        predictions_path = work_dir / "predictions.parquet"
        predictions.to_parquet(predictions_path, index=False)
        manifest = {
            "schema_version": "alphablocks.qlib-inference.v1",
            "job_id": job["job_id"],
            "model_id": job["model_id"],
            "model_version": int(config["planned_model_version"]),
            "model_kind": model_kind,
            "dataset_hash": job["dataset_hash"],
            "training_job_id": source["training_job_id"],
            "trade_date": trade_date,
            "data_cutoff": inference["data_cutoff"],
            "feature_cutoff_at": inference["feature_cutoff_at"],
            "feature_names": expected_names,
            "coverage": coverages,
            "medians_source": "training_manifest",
            "row_count": len(predictions),
            "future_function_guards": [
                "computed_at <= inference data_cutoff for source factor rows",
                "event_available_at <= signal date close",
                "historical index membership",
                "training-fitted medians only",
            ],
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
