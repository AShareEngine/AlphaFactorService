from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import threading
from typing import Any, Callable

from factor_service.factor_backtest import UNIVERSES
from factor_service.entity_field_feature import (
    is_entity_field_feature,
    validate_entity_field_feature_identity,
)
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
    "random_forest": {
        "n_estimators", "max_depth", "min_samples_split", "min_samples_leaf",
        "max_features", "num_threads", "loss", "seed", "deterministic", "verbosity",
    },
    "linear": {
        "alpha", "fit_intercept", "solver", "max_iter", "num_threads", "loss",
        "seed", "deterministic", "verbosity",
    },
    "mlp": {
        "learning_rate", "hidden_layers", "hidden_size", "layer_count",
        "max_steps", "batch_size",
        "early_stopping_rounds", "eval_steps", "weight_decay", "num_threads",
        "loss", "seed", "deterministic", "verbosity",
    },
    "lstm": {
        "learning_rate", "lookback_window", "hidden_size", "num_layers", "dropout",
        "max_steps", "batch_size", "early_stopping_rounds", "eval_steps",
        "weight_decay", "num_threads", "loss", "seed", "deterministic", "verbosity",
    },
    "gru": {
        "learning_rate", "lookback_window", "hidden_size", "num_layers", "dropout",
        "max_steps", "batch_size", "early_stopping_rounds", "eval_steps",
        "weight_decay", "num_threads", "loss", "seed", "deterministic", "verbosity",
    },
    "alstm": {
        "learning_rate", "lookback_window", "hidden_size", "num_layers", "dropout",
        "max_steps", "batch_size", "early_stopping_rounds", "eval_steps",
        "weight_decay", "num_threads", "loss", "seed", "deterministic", "verbosity",
    },
    "transformer": {
        "learning_rate", "lookback_window", "d_model", "nhead",
        "transformer_layers", "dim_feedforward", "dropout", "max_steps",
        "batch_size", "early_stopping_rounds", "eval_steps", "weight_decay",
        "num_threads", "loss", "seed", "deterministic", "verbosity",
    },
    "tabnet": {
        "learning_rate", "n_d", "n_a", "n_steps", "n_shared", "n_ind",
        "batch_size", "max_steps", "early_stopping_rounds", "pretrain",
        "num_threads", "loss", "seed", "deterministic", "verbosity",
    },
    "tcn": {
        "learning_rate", "lookback_window", "hidden_size", "kernel_size",
        "num_layers", "dropout", "max_steps", "batch_size",
        "early_stopping_rounds", "eval_steps", "weight_decay", "num_threads",
        "loss", "seed", "deterministic", "verbosity",
    },
    "nativetft": {
        "learning_rate", "lookback_window", "d_model", "nhead",
        "gru_hidden_size", "num_layers", "dim_feedforward", "dropout",
        "max_steps", "batch_size", "early_stopping_rounds", "eval_steps",
        "weight_decay", "num_threads", "loss", "seed", "deterministic", "verbosity",
    },
    "transformer_lstm": {
        "learning_rate", "lookback_window", "d_model", "nhead",
        "transformer_layers", "dim_feedforward", "lstm_hidden_size", "lstm_layers",
        "dropout", "max_steps", "batch_size", "early_stopping_rounds", "eval_steps",
        "weight_decay", "num_threads", "loss", "seed", "deterministic", "verbosity",
    },
}
for _fields in MODEL_PARAM_FIELDS.values():
    _fields.update({"objective", "metric"})


def validate_job(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PermanentJobError("任务载荷必须是JSON对象")
    job_id = _identifier(payload.get("job_id"), "job_id")
    model_id = _identifier(payload.get("model_id"), "model_id")
    lease_token = str(payload.get("lease_token") or "").strip()
    if len(lease_token) < 16 or len(lease_token) > 512:
        raise PermanentJobError("lease_token长度无效")
    if str(payload.get("lease_owner") or "alpha-factor-service") != "alpha-factor-service":
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
    universe_id = str(spec.get("universe_id") or "").strip()
    index_code = str(spec.get("index_code") or "").strip()
    if universe_id not in UNIVERSES:
        raise PermanentJobError(f"不支持的股票池: {universe_id}")
    if index_code != UNIVERSES[universe_id]["index_code"]:
        raise PermanentJobError("dataset_spec的股票池与index_code不一致")
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
        if not isinstance(factor.get("params"), dict):
            raise PermanentJobError(f"因子{factor_id}缺少冻结params")
        if is_entity_field_feature(factor):
            try:
                validate_entity_field_feature_identity(factor)
            except ValueError as exc:
                raise PermanentJobError(str(exc)) from exc
        key = (factor_id, version, params_hash)
        if key in seen:
            raise PermanentJobError(f"因子{factor_id}重复")
        seen.add(key)
    materialization = spec.get("materialization")
    if not isinstance(materialization, dict) or materialization != {
        "mode": "on_demand", "format": "parquet", "persist_factor_values": False,
    }:
        raise PermanentJobError("数据集必须使用按需计算的不可变Parquet物化方式")
    coverage = float(spec.get("minimum_factor_coverage", 0.8))
    if not 0.5 <= coverage <= 1.0:
        raise PermanentJobError("minimum_factor_coverage必须在0.5到1之间")
    label_spec = spec.get("label") or {}
    if not isinstance(label_spec, dict):
        raise PermanentJobError("dataset_spec.label必须是对象")
    target_mode = str(
        spec.get("target_mode") or label_spec.get("mode") or "return"
    ).strip().lower()
    if target_mode not in {"return", "classification"}:
        raise PermanentJobError("目标类型只允许return或classification")
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
        raise PermanentJobError(
            "模型只允许lightgbm、xgboost、catboost、random_forest、linear、"
            "mlp、gru、lstm、alstm、transformer、tabnet、tcn、nativetft或transformer_lstm"
        )
    params = model.get("params") or {}
    if not isinstance(params, dict):
        raise PermanentJobError(f"{model_kind}参数必须是对象")
    unknown_params = sorted(set(params) - MODEL_PARAM_FIELDS[model_kind])
    if unknown_params:
        raise PermanentJobError(
            f"{model_kind}参数包含未允许字段: {', '.join(unknown_params)}"
        )
    _validate_model_params(model_kind, params)
    execution = config.get("execution") or {"node_id": "local"}
    if not isinstance(execution, dict):
        raise PermanentJobError("execution必须是对象")
    execution_node_id = _identifier(
        execution.get("node_id") or "local", "execution.node_id",
    )
    execution_mode = str(execution.get("mode") or (
        "local" if execution_node_id == "local" else "remote_ssh_docker"
    ))
    expected_mode = "local" if execution_node_id == "local" else "remote_ssh_docker"
    if execution_mode != expected_mode:
        raise PermanentJobError("execution.mode与node_id不一致")
    _integer(params.get("num_threads", 4), "num_threads", 1, 32)
    expected_loss = "binary" if target_mode == "classification" else "mse"
    if str(params.get("loss", "mse")).strip().lower() != expected_loss:
        raise PermanentJobError(
            f"{target_mode}目标必须使用{expected_loss}损失"
        )
    expected_objective = "binary" if target_mode == "classification" else "regression"
    if str(params.get("objective", expected_objective)).strip().lower() != expected_objective:
        raise PermanentJobError("Objective必须与目标类型一致")
    metric = str(
        params.get("metric") or ("auc" if expected_objective == "binary" else "rmse")
    ).strip().lower()
    supported_metrics = (
        {"auc", "binary_logloss"}
        if expected_objective == "binary"
        else {"l2", "rmse", "mae"}
    )
    if metric not in supported_metrics:
        raise PermanentJobError("Metric必须与Objective一致")
    for field in ("seed", "feature_fraction_seed", "bagging_seed", "data_random_seed"):
        if field not in params and field != "seed":
            continue
        _integer(params.get(field, 42), field, 0, 2_147_483_647)
    if params.get("deterministic", True) is not True:
        raise PermanentJobError("训练必须启用deterministic")
    _validate_walk_forward(config.get("walk_forward") or {})
    if config.get("incremental_training"):
        _validate_incremental_training(
            config["incremental_training"],
            model_id=model_id,
            planned_version=version,
            model_kind=model_kind,
            dataset_spec=spec,
            walk_forward=config.get("walk_forward") or {},
        )
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
    deep_kinds = {
        "mlp", "gru", "lstm", "alstm", "transformer", "tabnet", "tcn",
        "nativetft", "transformer_lstm",
    }
    default_lr = 0.001 if kind in deep_kinds else 0.05
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
    elif kind == "random_forest":
        _integer(params.get("n_estimators", 500), "n_estimators", 1, 100_000)
        _integer(params.get("max_depth", 0), "max_depth", 0, 128)
        _integer(params.get("min_samples_split", 2), "min_samples_split", 2, 1_000_000)
        _integer(params.get("min_samples_leaf", 1), "min_samples_leaf", 1, 1_000_000)
        _number(params.get("max_features", 1.0), "max_features", 0.000001, 1.0)
        return
    elif kind == "linear":
        _number(params.get("alpha", 1.0), "alpha", 0.0, 1_000_000.0)
        _integer(params.get("max_iter", 1000), "max_iter", 1, 1_000_000)
        if not isinstance(params.get("fit_intercept", False), bool):
            raise PermanentJobError("fit_intercept必须是布尔值")
        if str(params.get("solver") or "auto") not in {
            "auto", "svd", "cholesky", "lsqr", "sparse_cg", "sag", "saga", "lbfgs",
        }:
            raise PermanentJobError("linear.solver无效")
        return
    elif kind == "mlp":
        layers = params.get("hidden_layers")
        if layers is None:
            _integer(params.get("hidden_size", 64), "hidden_size", 4, 4096)
            _integer(params.get("layer_count", 2), "layer_count", 1, 8)
        else:
            if not isinstance(layers, list) or not 1 <= len(layers) <= 8:
                raise PermanentJobError("hidden_layers必须是包含1到8层的数组")
            for index, width in enumerate(layers, start=1):
                _integer(width, f"hidden_layers[{index}]", 4, 4096)
        _integer(params.get("max_steps", 300), "max_steps", 1, 100_000)
        _integer(params.get("batch_size", 2048), "batch_size", 16, 1_000_000)
        _integer(params.get("eval_steps", 10), "eval_steps", 1, 10_000)
        _number(params.get("weight_decay", 0.0001), "weight_decay", 0.0, 1_000_000.0)
        _integer(params.get("early_stopping_rounds", 10), "early_stopping_rounds", 1, 10_000)
        return
    elif kind in {"gru", "lstm", "alstm"}:
        _integer(params.get("lookback_window", 60), "lookback_window", 2, 252)
        _integer(params.get("hidden_size", 128), "hidden_size", 4, 4096)
        _integer(params.get("num_layers", 2), "num_layers", 1, 8)
        _number(params.get("dropout", 0.2), "dropout", 0.0, 0.9)
        _integer(params.get("max_steps", 300), "max_steps", 1, 100_000)
        _integer(params.get("batch_size", 512), "batch_size", 16, 1_000_000)
        _integer(params.get("eval_steps", 10), "eval_steps", 1, 10_000)
        _number(params.get("weight_decay", 0.0001), "weight_decay", 0.0, 1_000_000.0)
        _integer(params.get("early_stopping_rounds", 10), "early_stopping_rounds", 1, 10_000)
        return
    elif kind in {"transformer", "nativetft"}:
        _integer(params.get("lookback_window", 60), "lookback_window", 2, 252)
        d_model = _integer(params.get("d_model", 64), "d_model", 8, 1024)
        nhead = _integer(params.get("nhead", 4), "nhead", 1, 32)
        if d_model % nhead != 0:
            raise PermanentJobError("d_model必须能被nhead整除")
        if kind == "transformer":
            _integer(params.get("transformer_layers", 2), "transformer_layers", 1, 8)
        else:
            _integer(params.get("gru_hidden_size", 64), "gru_hidden_size", 4, 4096)
            _integer(params.get("num_layers", 1), "num_layers", 1, 8)
        _integer(params.get("dim_feedforward", 256), "dim_feedforward", 16, 16_384)
        _number(params.get("dropout", 0.2), "dropout", 0.0, 0.9)
        _validate_deep_training_loop(params, default_batch_size=256)
        return
    elif kind == "tcn":
        _integer(params.get("lookback_window", 60), "lookback_window", 2, 252)
        _integer(params.get("hidden_size", 128), "hidden_size", 4, 4096)
        _integer(params.get("kernel_size", 5), "kernel_size", 2, 64)
        _integer(params.get("num_layers", 5), "num_layers", 1, 16)
        _number(params.get("dropout", 0.5), "dropout", 0.0, 0.9)
        _validate_deep_training_loop(params, default_batch_size=256)
        return
    elif kind == "tabnet":
        _integer(params.get("n_d", 64), "n_d", 4, 4096)
        _integer(params.get("n_a", 64), "n_a", 4, 4096)
        _integer(params.get("n_steps", 5), "n_steps", 1, 64)
        _integer(params.get("n_shared", 2), "n_shared", 0, 16)
        _integer(params.get("n_ind", 2), "n_ind", 0, 16)
        _integer(params.get("max_steps", 100), "max_steps", 1, 100_000)
        _integer(params.get("batch_size", 4096), "batch_size", 16, 1_000_000)
        _integer(params.get("early_stopping_rounds", 20), "early_stopping_rounds", 1, 10_000)
        if not isinstance(params.get("pretrain", False), bool):
            raise PermanentJobError("tabnet.pretrain必须是布尔值")
        return
    elif kind == "transformer_lstm":
        _integer(params.get("lookback_window", 60), "lookback_window", 2, 252)
        d_model = _integer(params.get("d_model", 64), "d_model", 8, 1024)
        nhead = _integer(params.get("nhead", 4), "nhead", 1, 32)
        if d_model % nhead != 0:
            raise PermanentJobError("d_model必须能被nhead整除")
        _integer(params.get("transformer_layers", 2), "transformer_layers", 1, 8)
        _integer(params.get("dim_feedforward", 256), "dim_feedforward", 16, 16_384)
        _integer(params.get("lstm_hidden_size", 128), "lstm_hidden_size", 4, 4096)
        _integer(params.get("lstm_layers", 1), "lstm_layers", 1, 8)
        _number(params.get("dropout", 0.2), "dropout", 0.0, 0.9)
        _integer(params.get("max_steps", 300), "max_steps", 1, 100_000)
        _integer(params.get("batch_size", 256), "batch_size", 16, 1_000_000)
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


def _validate_deep_training_loop(
    params: dict[str, Any], *, default_batch_size: int,
) -> None:
    _integer(params.get("max_steps", 300), "max_steps", 1, 100_000)
    _integer(params.get("batch_size", default_batch_size), "batch_size", 16, 1_000_000)
    _integer(params.get("eval_steps", 10), "eval_steps", 1, 10_000)
    _number(params.get("weight_decay", 0.0001), "weight_decay", 0.0, 1_000_000.0)
    _integer(params.get("early_stopping_rounds", 10), "early_stopping_rounds", 1, 10_000)


def _validate_walk_forward(source: Any) -> None:
    if not isinstance(source, dict):
        raise PermanentJobError("walk_forward必须是对象")
    if source.get("enabled", False) is not True:
        return
    strategy = str(source.get("strategy") or "rolling")
    if strategy not in {"rolling", "expanding"}:
        raise PermanentJobError("Walk-Forward策略只允许rolling或expanding")
    _integer(source.get("train_years", 3), "walk_forward.train_years", 1, 8)
    _integer(source.get("valid_months", 6), "walk_forward.valid_months", 1, 36)
    test_months = _integer(
        source.get("test_months", 12), "walk_forward.test_months", 1, 36,
    )
    step_months = _integer(
        source.get("step_months", 12), "walk_forward.step_months", 1, 36,
    )
    _integer(source.get("max_windows", 4), "walk_forward.max_windows", 1, 12)
    _integer(source.get("embargo_days", 5), "walk_forward.embargo_days", 1, 30)
    if step_months < test_months:
        raise PermanentJobError("Walk-Forward步长不得小于测试窗口")


def _validate_incremental_training(
    source: Any,
    *,
    model_id: str,
    planned_version: int,
    model_kind: str,
    dataset_spec: dict[str, Any],
    walk_forward: dict[str, Any],
) -> None:
    if not isinstance(source, dict):
        raise PermanentJobError("incremental_training必须是对象")
    if str(source.get("schema_version") or "") != "alphablocks.incremental-training.v1":
        raise PermanentJobError("增量训练schema_version无效")
    if str(source.get("mode") or "") != "lightgbm_append_trees_new_data_only":
        raise PermanentJobError("增量训练mode无效")
    if model_kind != "lightgbm":
        raise PermanentJobError("首版增量续训只支持LightGBM")
    if walk_forward.get("enabled") is True:
        raise PermanentJobError("增量续训暂不支持Walk-Forward")
    if _identifier(source.get("source_model_id"), "source_model_id") != model_id:
        raise PermanentJobError("增量训练来源模型ID不一致")
    source_version = _integer(
        source.get("source_model_version"), "source_model_version", 1, 1_000_000,
    )
    if source_version >= planned_version:
        raise PermanentJobError("增量训练来源版本必须早于目标版本")
    _identifier(source.get("source_job_id"), "source_job_id")
    source_end = _date(source.get("source_date_end"), "source_date_end")
    if source_end >= _date(dataset_spec.get("date_end"), "date_end"):
        raise PermanentJobError("增量训练数据集没有新增日期")
    _integer(
        source.get("minimum_new_trading_sessions", 60),
        "minimum_new_trading_sessions", 60, 504,
    )
    artifact = source.get("source_artifact")
    if not isinstance(artifact, dict):
        raise PermanentJobError("增量训练缺少来源模型产物")
    _identifier(artifact.get("artifact_id"), "source_artifact.artifact_id")
    digest = str(artifact.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PermanentJobError("增量训练来源模型产物SHA256无效")
    relative = Path(str(artifact.get("relative_path") or ""))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise PermanentJobError("增量训练来源模型产物路径无效")


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
