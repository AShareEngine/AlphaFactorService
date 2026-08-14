from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import threading
from typing import Any, Callable

from factor_service.research.errors import JobCanceled, PermanentJobError, WorkerShutdown


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HASH = re.compile(r"^[0-9a-f]{16,64}$")
MODEL_PARAM_FIELDS = {
    "lightgbm": {
        "learning_rate", "num_leaves", "max_depth", "n_estimators", "subsample",
        "colsample_bytree", "reg_alpha", "reg_lambda", "min_child_samples",
        "early_stopping_rounds", "num_threads", "loss", "seed",
        "feature_fraction_seed", "bagging_seed", "data_random_seed", "deterministic",
        "verbosity",
    },
    "xgboost": {
        "learning_rate", "max_depth", "n_estimators", "subsample",
        "colsample_bytree", "reg_alpha", "reg_lambda", "min_child_weight",
        "early_stopping_rounds", "num_threads", "loss", "seed", "deterministic",
        "verbosity",
    },
    "catboost": {
        "learning_rate", "depth", "n_estimators", "l2_leaf_reg", "random_strength",
        "early_stopping_rounds", "num_threads", "loss", "seed", "deterministic",
        "verbosity",
    },
    "mlp": {
        "learning_rate", "hidden_size", "layer_count", "max_steps", "batch_size",
        "early_stopping_rounds", "eval_steps", "weight_decay", "num_threads",
        "loss", "seed", "deterministic", "verbosity",
    },
}


def validate_job(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PermanentJobError("任务载荷必须是JSON对象")
    job_id = _identifier(payload.get("job_id"), "job_id")
    model_id = _identifier(payload.get("model_id"), "model_id")
    lease_token = str(payload.get("lease_token") or "").strip()
    if len(lease_token) < 16 or len(lease_token) > 512:
        raise PermanentJobError("lease_token长度无效")
    if str(payload.get("lease_owner") or "alpha-research-worker") != "alpha-research-worker":
        raise PermanentJobError("任务租约不属于AlphaFactorService研究调度进程")
    dataset_hash = str(payload.get("dataset_hash") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", dataset_hash):
        raise PermanentJobError("dataset_hash必须是64位SHA256")
    config = payload.get("config_json")
    if not isinstance(config, dict):
        raise PermanentJobError("任务缺少config_json")
    spec = payload.get("dataset_spec") or config.get("dataset")
    if not isinstance(spec, dict):
        raise PermanentJobError("任务缺少dataset_spec")
    canonical_hash = sha256(_canonical_json(spec).encode("utf-8")).hexdigest()
    if canonical_hash != dataset_hash:
        raise PermanentJobError("dataset_hash与冻结数据规格不一致")
    configured_spec = config.get("dataset")
    if not isinstance(configured_spec, dict) or _canonical_json(configured_spec) != _canonical_json(spec):
        raise PermanentJobError("config_json.dataset与冻结dataset_spec不一致")
    if str(spec.get("universe_id") or "") != "csi500" or str(spec.get("index_code") or "") != "000905.SH":
        raise PermanentJobError("首版只允许中证500历史时点股票池")
    kind = str(payload.get("kind") or "train")
    if kind not in {"train", "infer"}:
        raise PermanentJobError("任务kind只允许train或infer")
    start = _date(spec.get("date_start"), "date_start")
    end = _date(spec.get("date_end"), "date_end")
    if start >= end:
        raise PermanentJobError("训练开始日期必须早于结束日期")
    cutoff = _datetime(spec.get("data_cutoff"), "data_cutoff")
    factors = spec.get("factors")
    if not isinstance(factors, list) or not 1 <= len(factors) <= 100:
        raise PermanentJobError("因子数量必须在1到100之间")
    seen: set[tuple[str, int, str]] = set()
    for factor in factors:
        if not isinstance(factor, dict):
            raise PermanentJobError("因子规格必须是对象")
        factor_id = _identifier(factor.get("factor_id"), "factor_id")
        version = _integer(factor.get("factor_version"), "factor_version", 1, 1_000_000)
        params_hash = str(factor.get("params_hash") or "").strip().lower()
        if not HASH.fullmatch(params_hash):
            raise PermanentJobError(f"因子{factor_id}的params_hash无效")
        key = (factor_id, version, params_hash)
        if key in seen:
            raise PermanentJobError(f"因子{factor_id}重复")
        seen.add(key)
    coverage = float(spec.get("minimum_factor_coverage", 0.8))
    if not 0.5 <= coverage <= 1.0:
        raise PermanentJobError("minimum_factor_coverage必须在0.5到1之间")
    version = _integer(config.get("planned_model_version"), "planned_model_version", 1, 1_000_000)
    if kind == "infer":
        _validate_inference_config(config, model_id=model_id, version=version)
        result = dict(payload)
        result.update({
            "job_id": job_id, "model_id": model_id, "lease_token": lease_token,
            "dataset_hash": dataset_hash, "dataset_spec": dict(spec), "kind": kind,
        })
        result["config_json"] = dict(config)
        result["config_json"]["planned_model_version"] = version
        return result
    model = config.get("model")
    model_kind = str(model.get("kind") or "") if isinstance(model, dict) else ""
    if model_kind not in MODEL_PARAM_FIELDS:
        raise PermanentJobError("模型只允许lightgbm、xgboost、catboost或mlp")
    params = model.get("params") or {}
    if not isinstance(params, dict) or set(params) - MODEL_PARAM_FIELDS[model_kind]:
        raise PermanentJobError(f"{model_kind}参数包含未允许字段")
    _validate_model_params(model_kind, params)
    _integer(params.get("num_threads", 4), "num_threads", 1, 32)
    if str(params.get("loss", "mse")) != "mse":
        raise PermanentJobError("模型训练只允许MSE损失")
    for field in ("seed", "feature_fraction_seed", "bagging_seed", "data_random_seed"):
        if field not in params and field != "seed":
            continue
        _integer(params.get(field, 42), field, 0, 2_147_483_647)
    if params.get("deterministic", True) is not True:
        raise PermanentJobError("训练必须启用deterministic")
    result = dict(payload)
    result.update({
        "job_id": job_id, "model_id": model_id, "lease_token": lease_token,
        "dataset_hash": dataset_hash, "dataset_spec": dict(spec), "kind": kind,
    })
    result["config_json"] = dict(config)
    result["config_json"]["planned_model_version"] = version
    result["dataset_spec"]["data_cutoff"] = cutoff.isoformat()
    return result


def _validate_model_params(kind: str, params: dict[str, Any]) -> None:
    default_lr = 0.001 if kind == "mlp" else 0.05
    _number(params.get("learning_rate", default_lr), "learning_rate", 0.000001, 1.0)
    if kind == "lightgbm":
        _integer(params.get("num_leaves", 31), "num_leaves", 2, 65536)
        _integer(params.get("max_depth", -1), "max_depth", -1, 128)
        _integer(params.get("min_child_samples", 20), "min_child_samples", 1, 1_000_000)
    elif kind == "xgboost":
        _integer(params.get("max_depth", 6), "max_depth", 1, 128)
        _number(params.get("min_child_weight", 1.0), "min_child_weight", 0.0, 1_000_000.0)
    elif kind == "catboost":
        _integer(params.get("depth", 6), "depth", 1, 16)
        _number(params.get("l2_leaf_reg", 3.0), "l2_leaf_reg", 0.0, 1_000_000.0)
        _number(params.get("random_strength", 1.0), "random_strength", 0.0, 1_000_000.0)
    else:
        _integer(params.get("hidden_size", 64), "hidden_size", 4, 4096)
        _integer(params.get("layer_count", 2), "layer_count", 1, 8)
        _integer(params.get("max_steps", 300), "max_steps", 1, 100_000)
        _integer(params.get("batch_size", 2048), "batch_size", 16, 1_000_000)
        _integer(params.get("eval_steps", 10), "eval_steps", 1, 10_000)
        _number(params.get("weight_decay", 0.0001), "weight_decay", 0.0, 1_000_000.0)
        _integer(params.get("early_stopping_rounds", 10), "early_stopping_rounds", 1, 10_000)
        return
    _integer(params.get("n_estimators", 1000), "n_estimators", 1, 100_000)
    _integer(params.get("early_stopping_rounds", 50), "early_stopping_rounds", 1, 10_000)
    if kind in {"lightgbm", "xgboost"}:
        _number(params.get("subsample", 0.9), "subsample", 0.01, 1.0)
        _number(params.get("colsample_bytree", 0.9), "colsample_bytree", 0.01, 1.0)
        _number(params.get("reg_alpha", 0.0), "reg_alpha", 0.0, 1_000_000.0)
        _number(params.get("reg_lambda", 1.0 if kind == "xgboost" else 0.0), "reg_lambda", 0.0, 1_000_000.0)


def _validate_inference_config(config: dict[str, Any], *, model_id: str, version: int) -> None:
    if str(config.get("schema_version") or "") != "alphablocks.model-inference.v1":
        raise PermanentJobError("每日推理schema_version无效")
    source = config.get("source_model")
    inference = config.get("inference")
    if not isinstance(source, dict) or not isinstance(inference, dict):
        raise PermanentJobError("每日推理缺少source_model或inference规格")
    if _identifier(source.get("model_id"), "source_model.model_id") != model_id:
        raise PermanentJobError("每日推理源模型ID不一致")
    if _integer(source.get("model_version"), "source_model.model_version", 1, 1_000_000) != version:
        raise PermanentJobError("每日推理源模型版本不一致")
    _identifier(source.get("training_job_id"), "source_model.training_job_id")
    _identifier(source.get("artifact_id"), "source_model.artifact_id")
    digest = str(source.get("artifact_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PermanentJobError("每日推理模型产物SHA256无效")
    trade_date = _date(inference.get("trade_date"), "inference.trade_date")
    cutoff = _datetime(inference.get("data_cutoff"), "inference.data_cutoff")
    feature_cutoff = _datetime(inference.get("feature_cutoff_at"), "inference.feature_cutoff_at")
    if feature_cutoff.date() != trade_date:
        raise PermanentJobError("feature_cutoff_at必须属于目标交易日")
    if cutoff < feature_cutoff:
        raise PermanentJobError("data_cutoff不能早于feature_cutoff_at")


def safe_job_dir(work_root: Path, job_id: str) -> Path:
    clean = _identifier(job_id, "job_id")
    jobs_root = (Path(work_root) / "jobs").resolve()
    candidate = (jobs_root / clean).resolve()
    if candidate.parent != jobs_root:
        raise PermanentJobError("任务工作目录越界")
    return candidate


class CancellationToken:
    def __init__(self, shutdown_event: threading.Event | None = None) -> None:
        self._cancel = threading.Event()
        self._shutdown = shutdown_event or threading.Event()
        self.reason = ""

    def cancel(self, reason: str = "任务已请求取消") -> None:
        self.reason = reason
        self._cancel.set()

    @property
    def canceled(self) -> bool:
        return self._cancel.is_set()

    def checkpoint(self) -> None:
        if self._cancel.is_set():
            raise JobCanceled(self.reason or "任务已请求取消")
        if self._shutdown.is_set():
            raise WorkerShutdown("调度服务正在关闭，任务将重新排队")


ProgressCallback = Callable[[str, int, dict[str, Any]], None]


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not IDENTIFIER.fullmatch(text):
        raise PermanentJobError(f"{field}格式无效")
    return text


def _date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise PermanentJobError(f"{field}不是有效日期") from exc


def _datetime(value: Any, field: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PermanentJobError(f"{field}不是有效时间") from exc
    if result.tzinfo is None:
        raise PermanentJobError(f"{field}必须包含时区")
    return result


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PermanentJobError(f"{field}必须是整数") from exc
    if not minimum <= result <= maximum:
        raise PermanentJobError(f"{field}必须在{minimum}到{maximum}之间")
    return result


def _number(value: Any, field: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PermanentJobError(f"{field}必须是数字") from exc
    if not minimum <= result <= maximum:
        raise PermanentJobError(f"{field}必须在{minimum}到{maximum}之间")
    return result


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["CancellationToken", "ProgressCallback", "safe_job_dir", "validate_job"]
