from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from itertools import product
import json
import math
import re
import secrets
from typing import Any, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from factor_service.control_database import ControlDatabase, get_control_database
from factor_service.factor_backtest import UNIVERSES
from factor_service.entity_field_feature import (
    is_entity_field_feature,
    normalize_entity_field_feature,
)
from factor_service.model_validation import select_model_trial, select_parameter_trial
from factor_service.research.dataset import SW2021_INDUSTRY_SAFE_START
from factor_service.research.sample_filter_formula import (
    normalize_custom_sample_filters,
)


ACTIVE_STATUSES = frozenset({"leased", "running", "uploading"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})
MAX_EXPERIMENT_TRIALS = 24
MAX_EXPERIMENT_ITERATIONS = 3
MAX_LINEAGE_TRIALS = 24

CLASSICAL_STACKING_KINDS = frozenset({
    "lightgbm", "xgboost", "catboost", "random_forest", "linear",
})
DEEP_STACKING_KINDS = frozenset({
    "mlp", "gru", "lstm", "alstm", "transformer", "tabnet", "tcn",
    "nativetft", "transformer_lstm",
})

SEARCHABLE_MODEL_PARAMS: dict[str, frozenset[str]] = {
    "lightgbm": frozenset({
        "learning_rate", "num_leaves", "max_depth", "n_estimators",
        "subsample", "colsample_bytree", "reg_alpha", "reg_lambda",
        "min_child_samples", "min_data_in_leaf", "path_smooth",
        "bagging_freq", "lambda_l1", "lambda_l2", "feature_fraction",
        "bagging_fraction",
    }),
    "xgboost": frozenset({
        "learning_rate", "max_depth", "n_estimators", "subsample",
        "colsample_bytree", "reg_alpha", "reg_lambda", "min_child_weight",
    }),
    "catboost": frozenset({
        "learning_rate", "depth", "n_estimators", "l2_leaf_reg",
        "random_strength", "bagging_temperature", "od_wait",
    }),
    "random_forest": frozenset({
        "n_estimators", "max_depth", "min_samples_split", "min_samples_leaf",
        "max_features",
    }),
    "linear": frozenset({
        "alpha", "fit_intercept", "solver", "max_iter",
    }),
    "mlp": frozenset({
        "learning_rate", "hidden_layers", "hidden_size", "layer_count",
        "max_steps", "batch_size", "weight_decay",
    }),
    "gru": frozenset({
        "learning_rate", "lookback_window", "hidden_size", "num_layers",
        "dropout", "max_steps", "batch_size", "weight_decay",
    }),
    "lstm": frozenset({
        "learning_rate", "lookback_window", "hidden_size", "num_layers",
        "dropout", "max_steps", "batch_size", "weight_decay",
    }),
    "alstm": frozenset({
        "learning_rate", "lookback_window", "hidden_size", "num_layers",
        "dropout", "max_steps", "batch_size", "weight_decay",
    }),
    "transformer": frozenset({
        "learning_rate", "lookback_window", "d_model", "nhead",
        "transformer_layers", "dim_feedforward", "dropout", "max_steps",
        "batch_size", "weight_decay",
    }),
    "tabnet": frozenset({
        "learning_rate", "n_d", "n_a", "n_steps", "n_shared", "n_ind",
        "batch_size", "max_steps", "pretrain",
    }),
    "tcn": frozenset({
        "learning_rate", "lookback_window", "hidden_size", "kernel_size",
        "num_layers", "dropout", "max_steps", "batch_size", "weight_decay",
    }),
    "nativetft": frozenset({
        "learning_rate", "lookback_window", "d_model", "nhead",
        "gru_hidden_size", "num_layers", "dim_feedforward", "dropout",
        "max_steps", "batch_size", "weight_decay",
    }),
    "transformer_lstm": frozenset({
        "learning_rate", "lookback_window", "d_model", "nhead",
        "transformer_layers", "dim_feedforward", "lstm_hidden_size",
        "lstm_layers", "dropout", "max_steps", "batch_size", "weight_decay",
    }),
}


class ModelResearchError(ValueError):
    pass


class ModelResearchNotFound(ModelResearchError):
    pass


class ModelResearchConflict(ModelResearchError):
    pass


class ModelResearchRepository:
    def __init__(self, database: ControlDatabase | None = None) -> None:
        self.database = database or get_control_database()

    def create_training_job(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        spec = _dataset_spec(payload.get("dataset") or {})
        model = _model_spec(
            payload.get("model") or {}, target_mode=spec["target_mode"],
        )
        walk_forward = _walk_forward_spec(payload.get("walk_forward") or {})
        execution = _execution_spec(
            payload.get("execution") or {
                "node_id": payload.get("execution_node_id") or "local",
            }
        )
        research_origin = self._resolve_research_origin(
            payload.get("research_origin") or {},
            dataset=spec,
            model=model,
            walk_forward=walk_forward,
        )
        spec_json = _canonical_json(spec)
        spec_hash = sha256(spec_json.encode("utf-8")).hexdigest()
        dataset_id = f"dataset_{spec_hash[:24]}"
        job_id = f"model_job_{uuid4().hex}"
        model_id = _clean_identifier(
            str(payload.get("model_id") or ""),
            default=f"model_{uuid4().hex[:16]}",
        )
        incremental_training: dict[str, Any] = {}
        incremental_source = payload.get("incremental_from") or {}
        if incremental_source:
            if not isinstance(incremental_source, Mapping):
                raise ModelResearchError("incremental_from必须是对象")
            source_model_id = _required_identifier(
                str(incremental_source.get("model_id") or ""),
                "incremental_from.model_id",
            )
            source_version = int(incremental_source.get("model_version") or 0)
            if source_version <= 0:
                raise ModelResearchError("incremental_from.model_version无效")
            if model_id != source_model_id:
                raise ModelResearchConflict("增量训练必须写入来源模型的下一个版本")
            incremental_training = self._resolve_incremental_training(
                source_model_id, source_version,
                dataset=spec, model=model, walk_forward=walk_forward,
            )
        title = str(payload.get("title") or f"{model['kind']} 因子模型").strip()[:160]
        config = {
            "schema_version": "alphablocks.model-training.v1",
            "dataset": spec,
            "model": model,
            "walk_forward": walk_forward,
            "execution": execution,
            "backtest": {
                "universe_id": spec["universe_id"],
                "top_n": 20,
                "rebalance_every": 5,
                "benchmark_code": spec.get("benchmark_code") or spec["index_code"],
            },
        }
        experiment = _experiment_ref(payload.get("experiment") or {})
        if experiment:
            config["experiment"] = experiment
        if research_origin:
            config["research_origin"] = research_origin
        if incremental_training:
            config["incremental_training"] = incremental_training
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO model_dataset_specs(
                        dataset_id, spec_hash, name, universe_id, factor_count,
                        data_cutoff, spec_json, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (spec_hash) DO NOTHING
                    """,
                    (
                        dataset_id,
                        spec_hash,
                        str(spec.get("name") or title),
                        spec["universe_id"],
                        len(spec["factors"]),
                        spec["data_cutoff"],
                        Jsonb(spec),
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT dataset_id FROM model_dataset_specs WHERE spec_hash = %s",
                    (spec_hash,),
                ).fetchone()
                dataset_id = str(row["dataset_id"])
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"model:{model_id}",),
                )
                version_row = conn.execute(
                    """
                    SELECT GREATEST(
                        COALESCE((SELECT max(version) FROM model_versions WHERE model_id = %s), 0),
                        COALESCE((SELECT max((config_json ->> 'planned_model_version')::integer)
                                  FROM model_jobs
                                  WHERE model_id = %s AND status NOT IN ('failed', 'canceled')), 0)
                    ) + 1 AS version
                    """,
                    (model_id, model_id),
                ).fetchone()
                config["planned_model_version"] = int(version_row["version"])
                conn.execute(
                    """
                    INSERT INTO model_jobs(
                        job_id, dataset_id, model_id, kind, model_kind, title,
                        status, config_json, requested_at, updated_at
                    ) VALUES (%s, %s, %s, 'train', %s, %s,
                              'queued', %s, %s, %s)
                    """,
                    (job_id, dataset_id, model_id, model["kind"], title, Jsonb(config), now, now),
                )
                self._event(
                    conn, job_id, "job.queued", stage="queued",
                    payload={
                        "dataset_hash": spec_hash,
                        "experiment_id": experiment.get("experiment_id", ""),
                        "trial_index": experiment.get("trial_index"),
                        "trial_count": experiment.get("trial_count"),
                    },
                )
        return self.get_job(job_id)

    def incremental_training_precheck(
        self, model_id: str, version: int, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        dataset = _dataset_spec(payload.get("dataset") or {})
        model = _model_spec(
            payload.get("model") or {}, target_mode=dataset["target_mode"],
        )
        walk_forward = _walk_forward_spec(payload.get("walk_forward") or {})
        source_model = self.get_model(model_id, version)
        bundle = next((
            item for item in self.list_artifacts(str(source_model["job_id"]))
            if str(item.get("artifact_kind") or "") == "bundle"
        ), None)
        return _incremental_training_assessment(
            source_model,
            bundle,
            dataset=dataset,
            model=model,
            walk_forward=walk_forward,
        )

    def _resolve_incremental_training(
        self,
        model_id: str,
        version: int,
        *,
        dataset: Mapping[str, Any],
        model: Mapping[str, Any],
        walk_forward: Mapping[str, Any],
    ) -> dict[str, Any]:
        source_model = self.get_model(model_id, version)
        bundle = next((
            item for item in self.list_artifacts(str(source_model["job_id"]))
            if str(item.get("artifact_kind") or "") == "bundle"
        ), None)
        assessment = _incremental_training_assessment(
            source_model,
            bundle,
            dataset=dataset,
            model=model,
            walk_forward=walk_forward,
        )
        if assessment["passed"] is not True:
            failed = [
                str(item["label"])
                for item in assessment["checks"] if item["passed"] is not True
            ]
            raise ModelResearchConflict("增量训练准入未通过：" + "、".join(failed))
        return dict(assessment["contract"])

    def _resolve_research_origin(
        self,
        source: Mapping[str, Any],
        *,
        dataset: Mapping[str, Any],
        model: Mapping[str, Any],
        walk_forward: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Resolve and verify a historical configuration reuse request.

        The browser only declares where the configuration came from.  The
        control service resolves that source again and decides whether the new
        task is an exact replay or a derived study.  This prevents a changed
        task from being presented as a reproducibility run.
        """
        if not source:
            return {}
        if not isinstance(source, Mapping):
            raise ModelResearchError("research_origin必须是对象")
        source_type = str(source.get("source_type") or "").strip().lower()
        if source_type not in {"experiment", "model_version"}:
            raise ModelResearchError(
                "research_origin.source_type只支持experiment或model_version"
            )
        source_id = _required_identifier(
            str(source.get("source_id") or ""), "research_origin.source_id",
        )
        source_job_id = _required_identifier(
            str(source.get("source_job_id") or ""),
            "research_origin.source_job_id",
        )
        if source_type == "experiment":
            summary = self.get_training_experiment(source_id)
            selection = dict(summary.get("selection") or {})
            if (
                str(selection.get("status") or "") != "selected"
                or str(selection.get("selected_job_id") or "") != source_job_id
            ):
                raise ModelResearchConflict(
                    "只能复用实验中由验证集选出的入选任务"
                )
            source_model_id = str(selection.get("selected_model_id") or "")
            source_model_version = int(
                selection.get("selected_model_version") or 0
            )
        else:
            source_model_id = _required_identifier(
                str(source.get("source_model_id") or ""),
                "research_origin.source_model_id",
            )
            try:
                source_model_version = int(source.get("source_model_version") or 0)
            except (TypeError, ValueError) as exc:
                raise ModelResearchError(
                    "research_origin.source_model_version必须是正整数"
                ) from exc
            if source_model_version <= 0:
                raise ModelResearchError(
                    "research_origin.source_model_version必须是正整数"
                )
            source_model = self.get_model(source_model_id, source_model_version)
            if str(source_model.get("job_id") or "") != source_job_id:
                raise ModelResearchConflict("模型版本与来源训练任务不匹配")
        source_job = self.get_job(source_job_id)
        if str(source_job.get("status") or "") != "succeeded":
            raise ModelResearchConflict("只能复用已经成功完成的训练任务")
        if str(source_job.get("model_id") or "") != source_model_id:
            raise ModelResearchConflict("来源任务与来源模型不匹配")
        if source_model_version and int(source_job.get("model_version") or 0) != source_model_version:
            raise ModelResearchConflict("来源任务与来源模型版本不匹配")
        return _research_origin_spec(
            source,
            source_type=source_type,
            source_id=source_id,
            source_job=source_job,
            source_model_id=source_model_id,
            source_model_version=source_model_version,
            dataset=dataset,
            model=model,
            walk_forward=walk_forward,
        )

    def create_training_experiment(
        self, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create a parameter, label-horizon, or frozen-factor ablation study."""
        dataset_source = dict(payload.get("dataset") or {})
        walk_forward = _walk_forward_spec(payload.get("walk_forward") or {})
        model_source = dict(payload.get("model") or {})
        search = dict(payload.get("search") or {})
        strategy = str(search.get("strategy") or "grid").strip().lower()
        if strategy not in {"grid", "horizon_grid", "factor_ablation", "model_ensemble"}:
            raise ModelResearchError(
                "实验策略只支持grid、horizon_grid、factor_ablation或model_ensemble"
            )
        experiment_id = f"model_experiment_{uuid4().hex}"
        parent_experiment_id = str(
            payload.get("parent_experiment_id") or ""
        ).strip()
        parent_job_id = str(payload.get("parent_job_id") or "").strip()
        if parent_experiment_id:
            parent_experiment_id = _required_identifier(
                parent_experiment_id, "parent_experiment_id",
            )
        if parent_job_id:
            parent_job_id = _required_identifier(parent_job_id, "parent_job_id")
        iteration = int(payload.get("iteration") or (2 if parent_experiment_id else 1))
        if not 1 <= iteration <= MAX_EXPERIMENT_ITERATIONS:
            raise ModelResearchError(
                f"实验迭代轮次必须在1到{MAX_EXPERIMENT_ITERATIONS}之间"
            )
        if not parent_experiment_id and iteration != 1:
            raise ModelResearchError("没有父实验时只能创建第1轮研究")
        lineage_prior_trial_count = 0
        experiment_title = str(
            payload.get("title") or {
                "horizon_grid": f"{model_source.get('kind') or 'model'} 多周期研究",
                "factor_ablation": f"{model_source.get('kind') or 'model'} 因子消融实验",
                "model_ensemble": "多模型对比研究",
            }.get(strategy, f"{model_source.get('kind') or 'model'} 参数实验")
        ).strip()[:160]
        model_id = _clean_identifier(
            str(payload.get("model_id") or ""),
            default=f"experiment_{experiment_id.removeprefix('model_experiment_')[:16]}",
        )
        if parent_experiment_id:
            parent = self._experiment_parent_context(
                parent_experiment_id, parent_job_id,
            )
            parent_summary = dict(parent["summary"])
            parent_job = dict(parent["job"])
            if strategy != "factor_ablation" or str(
                parent_summary.get("strategy") or ""
            ) != "factor_ablation":
                raise ModelResearchConflict("多轮继承目前只支持因子消融实验")
            expected_iteration = int(parent_summary.get("iteration") or 1) + 1
            if iteration != expected_iteration:
                raise ModelResearchConflict(
                    f"下一轮实验轮次必须为第{expected_iteration}轮"
                )
            if iteration > MAX_EXPERIMENT_ITERATIONS:
                raise ModelResearchConflict(
                    f"同一研究谱系最多允许{MAX_EXPERIMENT_ITERATIONS}轮，防止反复适配验证集"
                )
            if model_id != str(parent_job.get("model_id") or ""):
                raise ModelResearchConflict("下一轮实验必须沿用父任务的model_id")
            parent_dataset = _dataset_spec(parent_job.get("dataset_spec") or {})
            child_dataset = _dataset_spec(dataset_source)
            if _canonical_json(child_dataset) != _canonical_json(parent_dataset):
                raise ModelResearchConflict(
                    "下一轮实验必须完整继承父任务的冻结因子、样本范围、标签和data_cutoff"
                )
            parent_config = dict(parent_job.get("config_json") or {})
            if _canonical_json(_model_spec(model_source)) != _canonical_json(
                _model_spec(parent_config.get("model") or {})
            ):
                raise ModelResearchConflict("下一轮实验必须完整继承父任务的模型参数")
            if _canonical_json(walk_forward) != _canonical_json(
                _walk_forward_spec(parent_config.get("walk_forward") or {})
            ):
                raise ModelResearchConflict(
                    "下一轮实验必须完整继承父任务的Walk-Forward配置"
                )
            lineage_prior_trial_count = int(parent["trial_count"])
        if strategy == "horizon_grid":
            horizons = _horizon_search_values(search)
            model = _model_spec(model_source)
            normalized_trials = [
                {
                    "dataset": _dataset_spec({
                        **dataset_source,
                        "label_horizon_trading_days": horizon,
                    }),
                    "model": model,
                    "search_params": {
                        "label_horizon_trading_days": horizon,
                    },
                }
                for horizon in horizons
            ]
        elif strategy == "factor_ablation":
            model = _model_spec(model_source)
            normalized_trials = _factor_ablation_trials(dataset_source, search)
            for trial in normalized_trials:
                trial["model"] = model
        elif strategy == "model_ensemble":
            dataset = _dataset_spec(dataset_source)
            target_mode = str(dataset.get("target_mode") or "return")
            model_kinds = search.get("model_kinds")
            if not isinstance(model_kinds, list) or not 2 <= len(model_kinds) <= 8:
                raise ModelResearchError("model_ensemble需要选择2到8个模型")
            normalized_kinds = [str(kind).strip().lower() for kind in model_kinds]
            if len(set(normalized_kinds)) != len(normalized_kinds):
                raise ModelResearchError("model_ensemble不能重复选择同一个模型")
            params_by_kind = search.get("model_params_by_kind") or {}
            if not isinstance(params_by_kind, Mapping):
                raise ModelResearchError("model_params_by_kind必须是对象")
            base_models = []
            for trial_kind in normalized_kinds:
                params_source = params_by_kind.get(trial_kind) or {}
                if not isinstance(params_source, Mapping):
                    raise ModelResearchError(f"{trial_kind}的参数配置无效")
                base_models.append(_model_spec({
                    "kind": trial_kind, "params": params_source,
                }, target_mode=target_mode))
            ensemble_method = str(
                search.get("ensemble_method") or "none"
            ).strip().lower()
            if ensemble_method not in {"none", "stacking"}:
                raise ModelResearchError("集成方法只支持none或stacking")
            if ensemble_method == "stacking":
                family = _stacking_family(normalized_kinds)
                try:
                    n_folds = int(search.get("n_folds") or 3)
                    meta_alpha = float(search.get("meta_alpha") or 1.0)
                except (TypeError, ValueError) as exc:
                    raise ModelResearchError("Stacking参数格式无效") from exc
                if not 2 <= n_folds <= 10:
                    raise ModelResearchError("Stacking OOF折数必须在2到10之间")
                if not 0.01 <= meta_alpha <= 100.0:
                    raise ModelResearchError("Stacking元学习器alpha必须在0.01到100之间")
                normalized_trials = [{
                    "dataset": dataset,
                    "model": _model_spec({
                        "kind": "stacking",
                        "params": {
                            "n_folds": n_folds,
                            "meta_alpha": meta_alpha,
                        },
                        "base_models": base_models,
                    }, target_mode=target_mode),
                    "search_params": {
                        "model_kind": "stacking",
                        "ensemble_method": "stacking",
                        "stacking_family": family,
                        "base_model_kinds": normalized_kinds,
                        "n_folds": n_folds,
                        "meta_alpha": meta_alpha,
                    },
                }]
                experiment_title = str(
                    payload.get("title") or "Stacking 集成研究"
                ).strip()[:160]
            else:
                normalized_trials = [
                    {
                        "dataset": dataset,
                        "model": model,
                        "search_params": {"model_kind": model["kind"]},
                    }
                    for model in base_models
                ]
        else:
            dataset = _dataset_spec(dataset_source)
            trials = _grid_search_trials(model_source, search)
            # Validate every trial before writing any job so a bad combination
            # does not leave a partially-created experiment in the control DB.
            normalized_trials = [
                {
                    "dataset": dataset,
                    "model": _model_spec({
                        "kind": model_source.get("kind"), "params": params,
                    }),
                    "search_params": {},
                }
                for params in trials
            ]
            for trial in normalized_trials:
                trial["search_params"] = {
                    key: trial["model"]["params"][key]
                    for key in sorted(search.get("parameters") or {})
                }
        jobs: list[dict[str, Any]] = []
        trial_count = len(normalized_trials)
        if lineage_prior_trial_count + trial_count > MAX_LINEAGE_TRIALS:
            raise ModelResearchConflict(
                f"同一研究谱系累计最多允许{MAX_LINEAGE_TRIALS}组试验，防止验证集多重比较过拟合"
            )
        for index, trial in enumerate(normalized_trials, start=1):
            horizon = int(
                dict(trial["dataset"].get("label") or {}).get(
                    "horizon_trading_days"
                ) or 5
            )
            jobs.append(self.create_training_job({
                "title": (
                    f"{experiment_title} · T+{horizon}"
                    if strategy == "horizon_grid"
                    else f"{experiment_title} · {index}/{trial_count} · "
                    f"{trial['search_params']['removed_factor_id']}"
                    if strategy == "factor_ablation"
                    else f"{experiment_title} · {index}/{trial_count} · "
                    f"{trial['model']['kind']}"
                    if strategy == "model_ensemble"
                    else f"{experiment_title} · {index}/{trial_count}"
                ),
                "model_id": model_id,
                "dataset": trial["dataset"],
                "model": trial["model"],
                "walk_forward": {
                    **walk_forward,
                    "embargo_days": horizon,
                },
                "execution": payload.get("execution") or {
                    "node_id": payload.get("execution_node_id") or "local",
                },
                "research_origin": payload.get("research_origin") or {},
                "experiment": {
                    "experiment_id": experiment_id,
                    "title": experiment_title,
                    "strategy": strategy,
                    "parent_experiment_id": parent_experiment_id,
                    "parent_job_id": parent_job_id,
                    "iteration": iteration,
                    "lineage_prior_trial_count": lineage_prior_trial_count,
                    "lineage_trial_budget": MAX_LINEAGE_TRIALS,
                    "lineage_iteration_budget": MAX_EXPERIMENT_ITERATIONS,
                    "trial_index": index,
                    "trial_count": trial_count,
                    "search_params": trial["search_params"],
                    "auto_dispatch": True,
                },
            }))
        return _experiment_summary(experiment_id, jobs)

    def restart_training_experiment(self, experiment_id: str) -> dict[str, Any]:
        """Clone a terminal experiment into a fresh, auto-dispatched run.

        The failed experiment and its events remain immutable history.  Every
        normalized trial is copied verbatim so a retry cannot silently change
        the frozen dataset, model parameters, walk-forward contract, or node.
        """
        source = self.get_training_experiment(experiment_id)
        source_jobs = sorted(
            (dict(job) for job in source.get("jobs") or []),
            key=lambda job: int(
                dict((job.get("config_json") or {}).get("experiment") or {}).get(
                    "trial_index"
                )
                or 0
            ),
        )
        if not source_jobs:
            raise ModelResearchNotFound("待重试实验不存在")
        active = [
            str(job.get("job_id") or "")
            for job in source_jobs
            if str(job.get("status") or "") not in TERMINAL_STATUSES
        ]
        if active:
            raise ModelResearchConflict("实验仍有运行中任务，不能重复启动")
        if not any(
            str(job.get("status") or "") in {"failed", "canceled"}
            for job in source_jobs
        ):
            raise ModelResearchConflict("只有失败或已取消的实验可以重新训练")

        restarted_experiment_id = f"model_experiment_{uuid4().hex}"
        restarted_jobs: list[dict[str, Any]] = []
        for source_job in source_jobs:
            config = dict(source_job.get("config_json") or {})
            source_experiment = dict(config.get("experiment") or {})
            payload: dict[str, Any] = {
                "title": str(source_job.get("title") or source.get("title") or "重新训练"),
                "model_id": str(source_job.get("model_id") or source.get("model_id") or ""),
                "dataset": dict(source_job.get("dataset_spec") or config.get("dataset") or {}),
                "model": dict(config.get("model") or {}),
                "walk_forward": dict(config.get("walk_forward") or {}),
                "execution": dict(config.get("execution") or {"node_id": "local"}),
                "experiment": {
                    **source_experiment,
                    "experiment_id": restarted_experiment_id,
                    "auto_dispatch": True,
                },
            }
            if config.get("research_origin"):
                payload["research_origin"] = dict(config["research_origin"])
            incremental = dict(config.get("incremental_training") or {})
            if incremental.get("source_model_id") and incremental.get(
                "source_model_version"
            ):
                payload["incremental_from"] = {
                    "model_id": incremental["source_model_id"],
                    "model_version": incremental["source_model_version"],
                }
            restarted_jobs.append(self.create_training_job(payload))

        result = _experiment_summary(restarted_experiment_id, restarted_jobs)
        result["restarted_from_experiment_id"] = str(source["experiment_id"])
        return result

    def _experiment_parent_context(
        self, parent_experiment_id: str, parent_job_id: str,
    ) -> dict[str, Any]:
        if not parent_job_id:
            raise ModelResearchConflict("下一轮实验必须指定父实验的入选任务")
        seen: set[str] = set()
        lineage: list[dict[str, Any]] = []
        current_id = parent_experiment_id
        while current_id:
            if current_id in seen:
                raise ModelResearchConflict("实验谱系存在循环引用")
            seen.add(current_id)
            summary = self.get_training_experiment(current_id)
            lineage.append(summary)
            if len(lineage) > MAX_EXPERIMENT_ITERATIONS:
                raise ModelResearchConflict("实验谱系超过允许的最大迭代轮次")
            current_id = str(summary.get("parent_experiment_id") or "")
        direct_parent = lineage[0]
        selection = dict(direct_parent.get("selection") or {})
        selected_job_id = str(selection.get("selected_job_id") or "")
        if str(selection.get("status") or "") != "selected" or not selected_job_id:
            raise ModelResearchConflict("父实验尚未选出验证集最佳任务")
        if parent_job_id != selected_job_id:
            raise ModelResearchConflict("parent_job_id必须是父实验的验证集入选任务")
        parent_job = next((
            dict(job) for job in direct_parent.get("jobs") or []
            if str(job.get("job_id") or "") == parent_job_id
        ), None)
        if parent_job is None or str(parent_job.get("status") or "") != "succeeded":
            raise ModelResearchConflict("父实验入选任务不存在或尚未成功完成")
        return {
            "summary": direct_parent,
            "job": parent_job,
            "trial_count": sum(int(item.get("trial_count") or 0) for item in lineage),
        }

    def reserve_ensemble_model(
        self, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Reserve an immutable ensemble version before ClickHouse materialization."""
        source_refs = payload.get("sources")
        if (
            not isinstance(source_refs, list)
            or not 2 <= len(source_refs) <= 8
            or any(not isinstance(item, Mapping) for item in source_refs)
        ):
            raise ModelResearchError("融合模型必须选择2到8个有效源模型版本")
        source_models = [
            self.get_model(
                str((item or {}).get("model_id") or ""),
                int((item or {}).get("model_version") or (item or {}).get("version") or 0),
            )
            for item in source_refs
        ]
        ensemble = _ensemble_spec(payload, source_models)
        dataset = dict(ensemble.pop("dataset"))
        spec_json = _canonical_json(dataset)
        spec_hash = sha256(spec_json.encode("utf-8")).hexdigest()
        dataset_id = f"dataset_{spec_hash[:24]}"
        job_id = f"model_job_{uuid4().hex}"
        now = _utcnow()
        config = {
            "schema_version": "alphablocks.model-ensemble.v1",
            "dataset": dataset,
            "model": {"kind": "ensemble", "params": {}},
            "ensemble": ensemble,
            "backtest": {
                "universe_id": dataset["universe_id"],
                "top_n": 20,
                "rebalance_every": 5,
                "benchmark_code": dataset["index_code"],
            },
        }
        with self.database.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO model_dataset_specs(
                        dataset_id, spec_hash, name, universe_id, factor_count,
                        data_cutoff, spec_json, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (spec_hash) DO NOTHING
                    """,
                    (
                        dataset_id, spec_hash, dataset["name"],
                        dataset["universe_id"], len(dataset.get("factors") or []),
                        dataset["data_cutoff"], Jsonb(dataset), now,
                    ),
                )
                row = conn.execute(
                    "SELECT dataset_id FROM model_dataset_specs WHERE spec_hash = %s",
                    (spec_hash,),
                ).fetchone()
                dataset_id = str(row["dataset_id"])
                model_id = str(ensemble["model_id"])
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"model:{model_id}",),
                )
                version_row = conn.execute(
                    """
                    SELECT GREATEST(
                        COALESCE((SELECT max(version) FROM model_versions WHERE model_id = %s), 0),
                        COALESCE((SELECT max(model_version) FROM model_jobs
                                  WHERE model_id = %s AND status NOT IN ('failed', 'canceled')), 0)
                    ) + 1 AS version
                    """,
                    (model_id, model_id),
                ).fetchone()
                version = int(version_row["version"])
                config["planned_model_version"] = version
                conn.execute(
                    """
                    INSERT INTO model_jobs(
                        job_id, dataset_id, model_id, kind, model_kind, title,
                        status, config_json, attempt_count, max_attempts,
                        model_version, requested_at, started_at, updated_at
                    ) VALUES (%s, %s, %s, 'ensemble', 'ensemble', %s,
                              'running', %s, 1, 1, %s, %s, %s, %s)
                    """,
                    (
                        job_id, dataset_id, model_id, ensemble["name"],
                        Jsonb(config), version, now, now, now,
                    ),
                )
                self._event(
                    conn, job_id, "job.running", stage="materializing",
                    message="正在融合源模型历史预测",
                    payload={
                        "model_id": model_id, "model_version": version,
                        "source_count": len(ensemble["sources"]),
                        "weight_strategy": ensemble["weight_strategy"],
                    },
                )
        return self.get_job(job_id)

    def complete_ensemble_model(
        self, job_id: str, predictions: Mapping[str, Any],
        evaluation: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = _utcnow()
        prediction_summary = _json_ready_mapping(predictions)
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM model_jobs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("融合模型任务不存在")
                if str(row["status"]) == "succeeded":
                    return self.get_model(str(row["model_id"]), int(row["model_version"]))
                if str(row["kind"]) != "ensemble" or str(row["status"]) != "running":
                    raise ModelResearchConflict("融合模型任务状态不允许完成")
                config = dict(row["config_json"] or {})
                ensemble = dict(config.get("ensemble") or {})
                dataset = dict(config.get("dataset") or {})
                model_id = str(row["model_id"])
                version = int(row["model_version"] or 0)
                manifest = {
                    "schema_version": "alphablocks.model-manifest.v1",
                    "model_kind": "ensemble",
                    "artifactless": True,
                    "ensemble": ensemble,
                    "feature_names": [
                        f"{item['model_id']}__v{item['model_version']}__score"
                        for item in ensemble.get("sources") or []
                    ],
                    "future_function_guards": [
                        "每个源模型版本、数据集哈希和融合权重均已冻结",
                        "自动权重只读取源模型验证集ICIR，不读取测试集指标",
                        "只融合所有源模型共同存在且feature_cutoff不晚于T日15:00的预测",
                        "融合后按交易日重新排名并映射到[-1, 1]",
                    ],
                    "created_at": now.isoformat(),
                }
                metrics = {
                    **_json_ready_mapping(evaluation),
                    "evaluation_mode": "ensemble_oos",
                    "source_count": len(ensemble.get("sources") or []),
                    "selection_split": "validation",
                    "test_metrics_role": "report_only",
                }
                conn.execute(
                    """
                    INSERT INTO model_versions(
                        model_id, version, job_id, dataset_id, name, model_kind,
                        state, metrics_json, feature_importance_json,
                        prediction_json, manifest_json, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, 'ensemble', 'candidate',
                              %s, '[]'::jsonb, %s, %s, %s, %s)
                    """,
                    (
                        model_id, version, job_id, row["dataset_id"], row["title"],
                        Jsonb(metrics), Jsonb(prediction_summary), Jsonb(manifest),
                        now, now,
                    ),
                )
                result = {
                    "metrics": metrics,
                    "feature_importance": [],
                    "predictions": prediction_summary,
                    "manifest": manifest,
                }
                conn.execute(
                    """
                    UPDATE model_jobs
                    SET status = 'succeeded', result_json = %s,
                        finished_at = %s, updated_at = %s
                    WHERE job_id = %s
                    """,
                    (Jsonb(result), now, now, job_id),
                )
                self._event(
                    conn, job_id, "job.succeeded", stage="succeeded",
                    message="融合模型已注册",
                    payload={
                        "model_id": model_id, "model_version": version,
                        "prediction_rows": int(prediction_summary.get("row_count") or 0),
                    },
                )
        return self.get_model(model_id, version)

    def fail_ensemble_model(self, job_id: str, error_message: str) -> None:
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT status FROM model_jobs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                ).fetchone()
                if not row or str(row["status"]) != "running":
                    return
                conn.execute(
                    """
                    UPDATE model_jobs
                    SET status = 'failed', error_message = %s,
                        finished_at = %s, updated_at = %s
                    WHERE job_id = %s
                    """,
                    (str(error_message)[:4000], now, now, job_id),
                )
                self._event(
                    conn, job_id, "job.failed", stage="failed",
                    message=str(error_message)[:1000],
                )

    def record_ensemble_inference(
        self, model_id: str, version: int, *, trade_date: str,
        data_cutoff: str, predictions: Mapping[str, Any], trigger: str = "manual",
    ) -> dict[str, Any]:
        """Record a synchronous score-fusion run without dispatching a trainer."""
        model = self.get_model(model_id, version)
        if str(model.get("model_kind")) != "ensemble":
            raise ModelResearchConflict("当前模型不是融合模型")
        target_date = _iso_date(trade_date, "trade_date")
        cutoff = _iso_datetime(data_cutoff or _utcnow().isoformat(), "data_cutoff")
        prediction_result = _json_ready_mapping(predictions)
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"inference:{model_id}:{version}:{target_date}",),
                )
                existing = conn.execute(
                    """
                    SELECT job_id FROM model_jobs
                    WHERE model_id = %s AND model_version = %s AND kind = 'infer'
                      AND config_json -> 'inference' ->> 'trade_date' = %s
                      AND status = 'succeeded'
                    ORDER BY requested_at DESC LIMIT 1
                    """,
                    (model_id, int(version), target_date),
                ).fetchone()
                job_id = str(existing["job_id"]) if existing else f"model_job_{uuid4().hex}"
                config = {
                    "schema_version": "alphablocks.model-inference.v1",
                    "dataset": dict(model["dataset_spec"]),
                    "planned_model_version": int(version),
                    "source_model": {
                        "model_id": model_id,
                        "model_version": int(version),
                        "training_job_id": str(model["job_id"]),
                        "artifactless": True,
                    },
                    "inference": {
                        "trade_date": target_date,
                        "data_cutoff": cutoff,
                        "mode": "ensemble_score_fusion",
                        "trigger": (
                            "schedule" if str(trigger).lower() == "schedule" else "manual"
                        ),
                    },
                }
                result = {"predictions": prediction_result}
                if existing:
                    conn.execute(
                        """
                        UPDATE model_jobs
                        SET result_json = %s, updated_at = %s
                        WHERE job_id = %s
                        """,
                        (Jsonb(result), now, job_id),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO model_jobs(
                            job_id, dataset_id, model_id, kind, model_kind, title,
                            status, config_json, result_json, attempt_count,
                            max_attempts, model_version, requested_at, started_at,
                            finished_at, updated_at
                        ) VALUES (%s, %s, %s, 'infer', 'ensemble', %s, 'succeeded',
                                  %s, %s, 1, 1, %s, %s, %s, %s, %s)
                        """,
                        (
                            job_id, model["dataset_id"], model_id,
                            f"{model['name']} · {target_date}融合预测",
                            Jsonb(config), Jsonb(result), int(version),
                            now, now, now, now,
                        ),
                    )
                current_summary = dict(model.get("prediction_json") or {})
                current_summary.update({
                    "last_inference_run_id": prediction_result.get("inference_run_id"),
                    "last_inference_rows": int(prediction_result.get("row_count") or 0),
                    "last_inference_trade_date": target_date,
                    "last_inference_at": now.isoformat(),
                })
                if target_date >= str(current_summary.get("latest_trade_date") or ""):
                    current_summary.update({
                        "latest_trade_date": target_date,
                        "latest_cross_section_rows": int(
                            prediction_result.get("latest_cross_section_rows")
                            or prediction_result.get("row_count") or 0
                        ),
                        "latest_inference_run_id": prediction_result.get("inference_run_id"),
                        "latest_inference_rows": int(prediction_result.get("row_count") or 0),
                        "latest_inference_at": now.isoformat(),
                    })
                conn.execute(
                    """
                    UPDATE model_versions SET prediction_json = %s, updated_at = %s
                    WHERE model_id = %s AND version = %s
                    """,
                    (Jsonb(current_summary), now, model_id, int(version)),
                )
                self._event(
                    conn, job_id, "job.succeeded", stage="succeeded",
                    message="融合模型每日预测已生成",
                    payload={
                        "model_id": model_id, "model_version": int(version),
                        "trade_date": target_date,
                        "prediction_rows": int(prediction_result.get("row_count") or 0),
                    },
                )
        return self.get_job(job_id)

    def create_inference_job(
        self, model_id: str, version: int, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create one idempotent daily inference job for a registered model."""
        model = self.get_model(model_id, version)
        trade_date = _iso_date(payload.get("trade_date"), "trade_date")
        data_cutoff = _iso_datetime(
            payload.get("data_cutoff") or _utcnow().isoformat(), "data_cutoff",
        )
        signal_close = datetime.combine(
            datetime.fromisoformat(trade_date).date(),
            datetime.min.time().replace(hour=15),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ).astimezone(timezone.utc)
        if datetime.fromisoformat(data_cutoff) < signal_close:
            raise ModelResearchError("每日推理只能在目标交易日收盘后执行")
        artifacts = self.list_artifacts(str(model["job_id"]))
        bundle = next(
            (item for item in artifacts if str(item.get("artifact_kind")) == "bundle"),
            None,
        )
        if not bundle:
            raise ModelResearchConflict("模型缺少可下载的训练产物")
        job_id = f"model_job_{uuid4().hex}"
        title = str(
            payload.get("title")
            or f"{model.get('name') or model_id} · {trade_date}每日推理"
        ).strip()[:160]
        config = {
            "schema_version": "alphablocks.model-inference.v1",
            "dataset": dict(model["dataset_spec"]),
            "planned_model_version": int(version),
            "source_model": {
                "model_id": model_id,
                "model_version": int(version),
                "training_job_id": str(model["job_id"]),
                "artifact_id": str(bundle["artifact_id"]),
                "artifact_sha256": str(bundle["sha256"]),
                "artifact_file_name": str(bundle["file_name"]),
            },
            "inference": {
                "trade_date": trade_date,
                "data_cutoff": data_cutoff,
                "feature_cutoff_at": signal_close.isoformat(),
                "trigger": (
                    "schedule"
                    if str(payload.get("trigger") or "manual").lower() == "schedule"
                    else "manual"
                ),
            },
        }
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"inference:{model_id}:{version}:{trade_date}",),
                )
                existing = conn.execute(
                    """
                    SELECT job_id FROM model_jobs
                    WHERE model_id = %s AND model_version = %s AND kind = 'infer'
                      AND config_json -> 'inference' ->> 'trade_date' = %s
                      AND status NOT IN ('failed', 'canceled')
                    ORDER BY requested_at DESC LIMIT 1
                    """,
                    (model_id, int(version), trade_date),
                ).fetchone()
                if existing:
                    return self.get_job(str(existing["job_id"]))
                conn.execute(
                    """
                    INSERT INTO model_jobs(
                        job_id, dataset_id, model_id, kind, model_kind, title,
                        status, config_json, model_version, requested_at, updated_at
                    ) VALUES (%s, %s, %s, 'infer', %s, %s,
                              'queued', %s, %s, %s, %s)
                    """,
                    (
                        job_id, model["dataset_id"], model_id, model["model_kind"],
                        title, Jsonb(config), int(version), now, now,
                    ),
                )
                self._event(
                    conn, job_id, "job.queued", stage="queued",
                    payload={
                        "kind": "infer", "trade_date": trade_date,
                        "model_id": model_id, "model_version": int(version),
                    },
                )
        return self.get_job(job_id)

    def list_jobs(
        self, *, status: str = "", experiment_id: str = "", kind: str = "",
        model_id: str = "", model_version: int | None = None,
        trade_date: str = "", limit: int = 100,
    ) -> list[dict[str, Any]]:
        values: list[Any] = []
        clauses: list[str] = []
        if status:
            clauses.append("jobs.status = %s")
            values.append(status)
        if experiment_id:
            clauses.append("jobs.config_json -> 'experiment' ->> 'experiment_id' = %s")
            values.append(_required_identifier(experiment_id, "experiment_id"))
        if kind:
            clauses.append("jobs.kind = %s")
            values.append(_required_identifier(kind, "kind"))
        if model_id:
            clauses.append("jobs.model_id = %s")
            values.append(_required_identifier(model_id, "model_id"))
        if model_version is not None:
            clauses.append("jobs.model_version = %s")
            values.append(max(1, int(model_version)))
        if trade_date:
            clauses.append("jobs.config_json -> 'inference' ->> 'trade_date' = %s")
            values.append(_iso_date(trade_date, "trade_date"))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(int(limit), 500)))
        with self.database.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT jobs.*, specs.spec_hash AS dataset_hash,
                       specs.spec_json AS dataset_spec
                FROM model_jobs jobs
                JOIN model_dataset_specs specs USING(dataset_id)
                {where}
                ORDER BY jobs.requested_at DESC
                LIMIT %s
                """,
                tuple(values),
            ).fetchall()
        return [_job_row(row) for row in rows]

    def list_inference_runs(
        self, *, status: str = "", model_id: str = "",
        model_version: int | None = None, trade_date: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return compact inference history without the frozen dataset payload."""
        values: list[Any] = []
        clauses = ["jobs.kind = 'infer'"]
        if status:
            clauses.append("jobs.status = %s")
            values.append(_required_identifier(status, "status"))
        if model_id:
            clauses.append("jobs.model_id = %s")
            values.append(_required_identifier(model_id, "model_id"))
        if model_version is not None:
            clauses.append("jobs.model_version = %s")
            values.append(max(1, int(model_version)))
        if trade_date:
            clauses.append("jobs.config_json -> 'inference' ->> 'trade_date' = %s")
            values.append(_iso_date(trade_date, "trade_date"))
        values.append(max(1, min(int(limit), 500)))
        with self.database.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT jobs.job_id, jobs.model_id, jobs.model_version,
                       jobs.model_kind, jobs.title, jobs.status,
                       jobs.config_json, jobs.result_json, jobs.progress_json,
                       jobs.attempt_count, jobs.max_attempts,
                       jobs.cancel_requested, jobs.error_message,
                       jobs.requested_at, jobs.started_at, jobs.finished_at,
                       jobs.updated_at, versions.name AS model_name,
                       versions.state AS model_state,
                       COALESCE(versions.is_default, FALSE) AS is_default
                FROM model_jobs jobs
                LEFT JOIN model_versions versions
                  ON versions.model_id = jobs.model_id
                 AND versions.version = jobs.model_version
                WHERE {' AND '.join(clauses)}
                ORDER BY jobs.requested_at DESC
                LIMIT %s
                """,
                tuple(values),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = _job_row(row)
            config = dict(item.pop("config_json", {}) or {})
            inference = dict(config.get("inference") or {})
            job_result = dict(item.pop("result_json", {}) or {})
            predictions = dict(job_result.get("predictions") or {})
            progress = dict(item.pop("progress_json", {}) or {})
            item.update({
                "trade_date": str(inference.get("trade_date") or "")[:10],
                "data_cutoff": inference.get("data_cutoff"),
                "feature_cutoff_at": inference.get("feature_cutoff_at"),
                "trigger": str(inference.get("trigger") or "unknown"),
                "run_mode": str(inference.get("mode") or "model_inference"),
                "inference_run_id": str(
                    predictions.get("inference_run_id") or item["job_id"]
                ),
                "prediction_rows": int(
                    predictions.get("latest_cross_section_rows")
                    or predictions.get("row_count") or 0
                ),
                "stage": str(progress.get("stage") or item.get("status") or ""),
            })
            result.append(item)
        return result

    def get_training_experiment(self, experiment_id: str) -> dict[str, Any]:
        experiment_id = _required_identifier(experiment_id, "experiment_id")
        jobs = self.list_jobs(experiment_id=experiment_id, limit=MAX_EXPERIMENT_TRIALS)
        if not jobs:
            raise ModelResearchNotFound("参数实验不存在")
        return _experiment_summary(experiment_id, jobs)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.database.connection() as conn:
            row = conn.execute(
                """
                SELECT jobs.*, specs.spec_hash AS dataset_hash,
                       specs.spec_json AS dataset_spec
                FROM model_jobs jobs
                JOIN model_dataset_specs specs USING(dataset_id)
                WHERE jobs.job_id = %s
                """,
                (job_id,),
            ).fetchone()
        if not row:
            raise ModelResearchNotFound("模型任务不存在")
        return _job_row(row)

    def list_events(self, job_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        self.get_job(job_id)
        with self.database.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM model_job_events
                WHERE job_id = %s AND event_id > %s
                ORDER BY event_id
                LIMIT 1000
                """,
                (job_id, max(0, int(after))),
            ).fetchall()
        return [dict(row) for row in rows]

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM model_jobs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("模型任务不存在")
                if str(row["status"]) in TERMINAL_STATUSES:
                    return self.get_job(job_id)
                status = "canceled" if str(row["status"]) == "queued" else str(row["status"])
                conn.execute(
                    """
                    UPDATE model_jobs
                    SET status = %s, cancel_requested = TRUE,
                        finished_at = CASE WHEN %s = 'canceled' THEN %s ELSE finished_at END,
                        updated_at = %s
                    WHERE job_id = %s
                    """,
                    (status, status, now, now, job_id),
                )
                self._event(conn, job_id, "job.cancel_requested", stage=status)
        return self.get_job(job_id)

    def claim_specific_job(
        self, job_id: str, *, lease_seconds: int = 90,
    ) -> dict[str, Any]:
        """Atomically lease one queued job for the single research service."""
        lease_owner = "alpha-factor-service"
        lease_seconds = max(30, min(int(lease_seconds), 300))
        now = _utcnow()
        expires = now + timedelta(seconds=lease_seconds)
        token = secrets.token_urlsafe(32)
        with self.database.connection() as conn:
            with conn.transaction():
                self._recover_expired(conn, now)
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("alpha-factor-service",),
                )
                active = conn.execute(
                    """
                    SELECT job_id FROM model_jobs
                    WHERE lease_owner = %s
                      AND status IN ('leased', 'running', 'uploading')
                      AND lease_expires_at >= %s
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (lease_owner, now),
                ).fetchone()
                if active:
                    raise ModelResearchConflict("模型研究调度服务正在执行其他任务")
                row = conn.execute(
                    "SELECT * FROM model_jobs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("模型任务不存在")
                if str(row["status"]) != "queued" or bool(row["cancel_requested"]):
                    raise ModelResearchConflict("任务当前不可分配")
                if int(row["attempt_count"]) >= int(row["max_attempts"]):
                    raise ModelResearchConflict("任务已达到最大尝试次数")
                conn.execute(
                    """
                    UPDATE model_jobs
                    SET status = 'leased', lease_owner = %s, lease_token = %s,
                        lease_expires_at = %s, attempt_count = attempt_count + 1,
                        started_at = COALESCE(started_at, %s), updated_at = %s,
                        error_message = ''
                    WHERE job_id = %s
                    """,
                    (lease_owner, token, expires, now, now, job_id),
                )
                self._event(
                    conn, job_id, "job.leased", stage="leased",
                    payload={
                        "lease_expires_at": expires.isoformat(),
                        "dispatch_mode": "push",
                    },
                )
        claimed = self.get_job(job_id)
        claimed["lease_token"] = token
        return claimed

    def claim_next_experiment_job(
        self, *, lease_seconds: int = 90,
    ) -> dict[str, Any] | None:
        """Lease the next auto-dispatched research trial, never legacy jobs."""
        lease_owner = "alpha-factor-service"
        lease_seconds = max(30, min(int(lease_seconds), 300))
        now = _utcnow()
        expires = now + timedelta(seconds=lease_seconds)
        token = secrets.token_urlsafe(32)
        with self.database.connection() as conn:
            with conn.transaction():
                self._recover_expired(conn, now)
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("alpha-factor-service",),
                )
                active = conn.execute(
                    """
                    SELECT job_id FROM model_jobs
                    WHERE lease_owner = %s
                      AND status IN ('leased', 'running', 'uploading')
                      AND lease_expires_at >= %s
                    LIMIT 1 FOR UPDATE
                    """,
                    (lease_owner, now),
                ).fetchone()
                if active:
                    return None
                row = conn.execute(
                    """
                    SELECT * FROM model_jobs
                    WHERE status = 'queued' AND cancel_requested = FALSE
                      AND attempt_count < max_attempts
                      AND config_json -> 'experiment' ->> 'auto_dispatch' = 'true'
                    ORDER BY requested_at, job_id
                    LIMIT 1 FOR UPDATE SKIP LOCKED
                    """
                ).fetchone()
                if not row:
                    return None
                job_id = str(row["job_id"])
                conn.execute(
                    """
                    UPDATE model_jobs
                    SET status = 'leased', lease_owner = %s, lease_token = %s,
                        lease_expires_at = %s, attempt_count = attempt_count + 1,
                        started_at = COALESCE(started_at, %s), updated_at = %s,
                        error_message = ''
                    WHERE job_id = %s
                    """,
                    (lease_owner, token, expires, now, now, job_id),
                )
                self._event(
                    conn, job_id, "job.leased", stage="leased",
                    payload={
                        "lease_expires_at": expires.isoformat(),
                        "dispatch_mode": "experiment_queue",
                    },
                )
        claimed = self.get_job(job_id)
        claimed["lease_token"] = token
        return claimed

    def release_dispatch_lease(
        self, job_id: str, *, lease_token: str, error_message: str,
    ) -> dict[str, Any]:
        """Return a job to the queue when HTTP dispatch never reached the worker."""
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM model_jobs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("模型任务不存在")
                self._assert_lease(row, lease_token)
                conn.execute(
                    """
                    UPDATE model_jobs
                    SET status = 'queued', lease_owner = '', lease_token = '',
                        lease_expires_at = NULL,
                        attempt_count = GREATEST(attempt_count - 1, 0),
                        started_at = CASE WHEN attempt_count <= 1 THEN NULL ELSE started_at END,
                        error_message = %s, updated_at = %s
                    WHERE job_id = %s
                    """,
                    (str(error_message)[:4000], now, job_id),
                )
                self._event(
                    conn, job_id, "job.dispatch_failed", stage="queued",
                    message=str(error_message)[:1000],
                )
        return self.get_job(job_id)

    def renew_lease(
        self, job_id: str, *, lease_token: str, lease_seconds: int = 90,
        progress: Mapping[str, Any] | None = None,
        record_event: bool = False,
    ) -> dict[str, Any]:
        now = _utcnow()
        expires = now + timedelta(seconds=max(30, min(int(lease_seconds), 300)))
        with self.database.connection() as conn:
            with conn.transaction():
                payload = dict(progress or {})
                row = conn.execute(
                    """
                    UPDATE model_jobs
                    SET lease_expires_at = %s, progress_json = %s, updated_at = %s,
                        status = CASE WHEN status = 'leased' THEN 'running' ELSE status END
                    WHERE job_id = %s AND lease_owner = %s AND lease_token = %s
                      AND status IN ('leased', 'running', 'uploading')
                      AND cancel_requested = FALSE
                    RETURNING *
                    """,
                    (expires, Jsonb(payload), now, job_id, "alpha-factor-service", lease_token),
                ).fetchone()
                if row and record_event:
                    stage = str(payload.get("stage") or "progress")
                    self._event(
                        conn, job_id, "job.progress", stage=stage,
                        payload=payload,
                    )
        if not row:
            raise ModelResearchConflict("任务租约失效、已取消或不属于调度服务")
        return self.get_job(job_id)

    def worker_control(self, job_id: str, *, lease_token: str) -> dict[str, Any]:
        """Return cancellation and lease state without mutating the lease."""
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM model_jobs WHERE job_id = %s",
                (job_id,),
            ).fetchone()
        if not row:
            raise ModelResearchNotFound("模型任务不存在")
        if (
            str(row["lease_owner"]) != "alpha-factor-service"
            or str(row["lease_token"]) != lease_token
        ):
            raise ModelResearchConflict("任务租约不属于模型研究调度服务")
        return _job_row(row)

    def set_worker_stage(
        self, job_id: str, *, lease_token: str, stage: str,
        progress: Mapping[str, Any] | None = None, message: str = "",
    ) -> dict[str, Any]:
        if stage not in {"running", "uploading"}:
            raise ModelResearchError("调度阶段只允许running或uploading")
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    UPDATE model_jobs
                    SET status = %s, progress_json = %s, updated_at = %s
                    WHERE job_id = %s AND lease_owner = %s AND lease_token = %s
                      AND status IN ('leased', 'running', 'uploading')
                      AND cancel_requested = FALSE
                    RETURNING job_id
                    """,
                    (stage, Jsonb(dict(progress or {})), _utcnow(), job_id, "alpha-factor-service", lease_token),
                ).fetchone()
                if not row:
                    raise ModelResearchConflict("任务租约失效、已取消或不属于调度服务")
                self._event(conn, job_id, f"job.{stage}", stage=stage, message=message, payload=progress)
        return self.get_job(job_id)

    def complete_job(
        self, job_id: str, *, lease_token: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        if str(self.get_job(job_id).get("kind") or "train") == "infer":
            return self._complete_inference_job(job_id, lease_token, result)
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM model_jobs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("模型任务不存在")
                if str(row["status"]) == "succeeded":
                    return self.get_job(job_id)
                self._assert_lease(row, lease_token)
                if bool(row["cancel_requested"]):
                    raise ModelResearchConflict("任务已请求取消")
                model_id = str(row["model_id"])
                config = dict(row["config_json"] or {})
                version = int(config.get("planned_model_version") or 0)
                if version <= 0:
                    raise ModelResearchConflict("任务缺少预留模型版本")
                conn.execute(
                    """
                    UPDATE model_jobs
                    SET status = 'succeeded', result_json = %s, model_version = NULL,
                        lease_expires_at = NULL, finished_at = %s, updated_at = %s
                    WHERE job_id = %s
                    """,
                    (Jsonb(dict(result)), now, now, job_id),
                )
                self._event(
                    conn, job_id, "job.succeeded", stage="succeeded",
                    message="训练完成，等待用户确认入库",
                    payload={
                        "model_id": model_id,
                        "planned_model_version": version,
                        "registration_status": "pending_confirmation",
                    },
                )
        return self.get_job(job_id)

    def register_training_result(self, job_id: str) -> dict[str, Any]:
        """Register one completed training result only after explicit user confirmation."""
        current = self.get_job(job_id)
        if str(current.get("kind") or "train") != "train":
            raise ModelResearchConflict("只有训练任务结果可以确认入库")
        registration = dict(
            (current.get("result_json") or {}).get("registration") or {}
        )
        if str(registration.get("status") or "") == "declined":
            raise ModelResearchConflict("该训练结果已选择不入库，请重新训练")
        experiment = dict(
            (current.get("config_json") or {}).get("experiment") or {}
        )
        if experiment:
            experiment_id = str(experiment.get("experiment_id") or "")
            summary = self.get_training_experiment(experiment_id)
            selection = dict(summary.get("selection") or {})
            if str(selection.get("status") or "") != "selected":
                raise ModelResearchConflict("参数实验尚未选出可入库版本")
            if str(selection.get("selected_job_id") or "") != job_id:
                raise ModelResearchConflict("参数实验只允许验证集入选版本确认入库")

        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM model_jobs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("模型任务不存在")
                if str(row["kind"] or "train") != "train":
                    raise ModelResearchConflict("只有训练任务结果可以确认入库")
                if str(row["status"] or "") != "succeeded":
                    raise ModelResearchConflict("只有训练完成的任务可以确认入库")
                model_id = str(row["model_id"])
                config = dict(row["config_json"] or {})
                version = int(config.get("planned_model_version") or 0)
                if version <= 0:
                    raise ModelResearchConflict("任务缺少预留模型版本")
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"model:{model_id}",),
                )
                existing = conn.execute(
                    """
                    SELECT job_id FROM model_versions
                    WHERE model_id = %s AND version = %s
                    """,
                    (model_id, version),
                ).fetchone()
                if existing and str(existing["job_id"]) != job_id:
                    raise ModelResearchConflict("预留模型版本已被其他任务占用")
                result = dict(row["result_json"] or {})
                if not result:
                    raise ModelResearchConflict("训练结果尚未写入，不能确认入库")
                registration = dict(result.get("registration") or {})
                if str(registration.get("status") or "") == "declined":
                    raise ModelResearchConflict("该训练结果已选择不入库，请重新训练")
                if not existing:
                    conn.execute(
                        """
                        INSERT INTO model_versions(
                            model_id, version, job_id, dataset_id, name, model_kind,
                            state, metrics_json, feature_importance_json,
                            prediction_json, manifest_json, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, 'candidate', %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            model_id, version, job_id, row["dataset_id"], row["title"],
                            row["model_kind"], Jsonb(dict(result.get("metrics") or {})),
                            Jsonb(list(result.get("feature_importance") or [])),
                            Jsonb(dict(result.get("predictions") or {})),
                            Jsonb(dict(result.get("manifest") or {})), now, now,
                        ),
                    )
                conn.execute(
                    """
                    UPDATE model_jobs
                    SET model_version = %s, updated_at = %s
                    WHERE job_id = %s
                    """,
                    (version, now, job_id),
                )
                conn.execute(
                    "UPDATE model_artifacts SET model_version = %s WHERE job_id = %s",
                    (version, job_id),
                )
                if not existing:
                    self._event(
                        conn, job_id, "job.registered", stage="registered",
                        message="用户已确认训练结果入库",
                        payload={"model_id": model_id, "model_version": version},
                    )
        return self.get_job(job_id)

    def decline_training_result(self, job_id: str) -> dict[str, Any]:
        """Persist the user's decision to keep a completed result out of the registry."""
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM model_jobs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("模型任务不存在")
                if str(row["kind"] or "train") != "train":
                    raise ModelResearchConflict("只有训练任务结果可以选择不入库")
                if str(row["status"] or "") != "succeeded":
                    raise ModelResearchConflict("只有训练完成的任务可以选择不入库")
                if int(row.get("model_version") or 0) > 0:
                    raise ModelResearchConflict("该训练结果已经入库")
                result = dict(row["result_json"] or {})
                if not result:
                    raise ModelResearchConflict("训练结果尚未写入，不能选择不入库")
                registration = dict(result.get("registration") or {})
                if str(registration.get("status") or "") == "declined":
                    return self.get_job(job_id)
                result["registration"] = {
                    "status": "declined",
                    "decided_at": now.isoformat(),
                }
                conn.execute(
                    """
                    UPDATE model_jobs
                    SET result_json = %s, updated_at = %s
                    WHERE job_id = %s
                    """,
                    (Jsonb(result), now, job_id),
                )
                self._event(
                    conn, job_id, "job.registration_declined",
                    stage="registration_declined",
                    message="用户已选择训练结果不入库",
                    payload={"registration_status": "declined"},
                )
        return self.get_job(job_id)

    def _complete_inference_job(
        self, job_id: str, lease_token: str, result: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM model_jobs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("模型任务不存在")
                if str(row["status"]) == "succeeded":
                    return self.get_job(job_id)
                self._assert_lease(row, lease_token)
                if bool(row["cancel_requested"]):
                    raise ModelResearchConflict("任务已请求取消")
                model_id = str(row["model_id"])
                version = int(row.get("model_version") or 0)
                predictions = dict(result.get("predictions") or {})
                if version <= 0 or int(predictions.get("model_version") or 0) != version:
                    raise ModelResearchConflict("推理结果模型版本不一致")
                model = conn.execute(
                    "SELECT * FROM model_versions WHERE model_id = %s AND version = %s FOR UPDATE",
                    (model_id, version),
                ).fetchone()
                if not model:
                    raise ModelResearchNotFound("推理对应的模型版本不存在")
                prediction_summary = dict(model.get("prediction_json") or {})
                target_date = str(predictions.get("date_end") or predictions.get("trade_date") or "")[:10]
                if not target_date:
                    raise ModelResearchConflict("推理结果缺少交易日")
                latest_prior = conn.execute(
                    """
                    SELECT config_json -> 'inference' ->> 'trade_date' AS trade_date,
                           result_json -> 'predictions' AS predictions,
                           finished_at
                    FROM model_jobs
                    WHERE model_id = %s AND model_version = %s AND kind = 'infer'
                      AND status = 'succeeded'
                    ORDER BY config_json -> 'inference' ->> 'trade_date' DESC
                    LIMIT 1
                    """,
                    (model_id, version),
                ).fetchone()
                prediction_summary.update({
                    "last_inference_run_id": str(predictions.get("inference_run_id") or job_id),
                    "last_inference_rows": int(predictions.get("row_count") or 0),
                    "last_inference_trade_date": target_date,
                    "last_inference_at": now.isoformat(),
                })
                latest_date = target_date
                latest_predictions = predictions
                latest_at = now
                if latest_prior and str(latest_prior.get("trade_date") or "") > target_date:
                    latest_date = str(latest_prior["trade_date"])
                    latest_predictions = dict(latest_prior.get("predictions") or {})
                    latest_at = latest_prior.get("finished_at") or now
                prediction_summary.update({
                    "latest_trade_date": latest_date,
                    "latest_inference_run_id": str(
                        latest_predictions.get("inference_run_id") or job_id
                    ),
                    "latest_inference_rows": int(latest_predictions.get("row_count") or 0),
                    "latest_inference_at": latest_at.isoformat(),
                })
                conn.execute(
                    """
                    UPDATE model_versions
                    SET prediction_json = %s, updated_at = %s
                    WHERE model_id = %s AND version = %s
                    """,
                    (Jsonb(prediction_summary), now, model_id, version),
                )
                conn.execute(
                    """
                    UPDATE model_jobs
                    SET status = 'succeeded', result_json = %s,
                        lease_expires_at = NULL, finished_at = %s, updated_at = %s
                    WHERE job_id = %s
                    """,
                    (Jsonb(dict(result)), now, now, job_id),
                )
                self._event(
                    conn, job_id, "job.succeeded", stage="succeeded",
                    payload={
                        "kind": "infer", "model_id": model_id,
                        "model_version": version, "trade_date": target_date,
                        "prediction_rows": int(predictions.get("row_count") or 0),
                    },
                )
        return self.get_job(job_id)

    def fail_job(
        self, job_id: str, *, lease_token: str, error_message: str,
        retryable: bool = True,
    ) -> dict[str, Any]:
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM model_jobs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("模型任务不存在")
                self._assert_lease(row, lease_token)
                canceled = bool(row["cancel_requested"])
                can_retry = bool(retryable) and not canceled and int(row["attempt_count"]) < int(row["max_attempts"])
                status = "queued" if can_retry else ("canceled" if canceled else "failed")
                conn.execute(
                    """
                    UPDATE model_jobs
                    SET status = %s, error_message = %s, lease_owner = '',
                        lease_token = '', lease_expires_at = NULL,
                        finished_at = CASE WHEN %s IN ('failed', 'canceled') THEN %s ELSE NULL END,
                        updated_at = %s
                    WHERE job_id = %s
                    """,
                    (status, str(error_message)[:4000], status, now, now, job_id),
                )
                self._event(
                    conn, job_id, "job.retry_queued" if can_retry else f"job.{status}",
                    stage=status, message=str(error_message)[:1000],
                )
        return self.get_job(job_id)

    def list_models(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connection() as conn:
            rows = conn.execute(
                """
                SELECT versions.*, specs.spec_hash AS dataset_hash,
                       specs.spec_json AS dataset_spec,
                       jobs.config_json AS job_config_json
                FROM model_versions versions
                JOIN model_dataset_specs specs USING(dataset_id)
                JOIN model_jobs jobs ON jobs.job_id = versions.job_id
                ORDER BY versions.created_at DESC
                LIMIT %s
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["run_after_local"] = str(item.get("run_after_local") or "16:30")[:5]
            result.append(item)
        return result

    def get_model(self, model_id: str, version: int) -> dict[str, Any]:
        with self.database.connection() as conn:
            row = conn.execute(
                """
                SELECT versions.*, specs.spec_hash AS dataset_hash,
                       specs.spec_json AS dataset_spec,
                       jobs.config_json AS job_config_json
                FROM model_versions versions
                JOIN model_dataset_specs specs USING(dataset_id)
                JOIN model_jobs jobs ON jobs.job_id = versions.job_id
                WHERE versions.model_id = %s AND versions.version = %s
                """,
                (model_id, int(version)),
            ).fetchone()
        if not row:
            raise ModelResearchNotFound("模型版本不存在")
        return dict(row)

    def update_model_registry(
        self,
        model_id: str,
        version: int,
        *,
        action: str,
        validation_approved: bool,
        note: str = "",
    ) -> dict[str, Any]:
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {
            "set_default", "clear_default", "archive", "restore", "update_note",
        }:
            raise ModelResearchError(
                "模型池操作只支持set_default、clear_default、archive、restore或update_note"
            )
        clean_note = str(note or "").strip()[:1000]
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    SELECT versions.*, specs.spec_json AS dataset_spec
                    FROM model_versions versions
                    JOIN model_dataset_specs specs USING(dataset_id)
                    WHERE versions.model_id = %s AND versions.version = %s
                    FOR UPDATE OF versions
                    """,
                    (model_id, int(version)),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("模型版本不存在")
                current = dict(row)
                current_state = str(current.get("state") or "candidate")
                scope = str(current.get("registry_scope") or "").strip()
                if not scope:
                    dataset = dict(current.get("dataset_spec") or {})
                    scope = (
                        f"{dataset.get('research_target') or 'stock_selection'}:"
                        f"{dataset.get('universe_id') or 'csi500'}"
                    )

                if normalized_action == "set_default":
                    if current_state == "archived":
                        raise ModelResearchConflict("已归档模型必须先恢复，才能设为主模型")
                    if not validation_approved:
                        raise ModelResearchConflict("只有通过研究门槛的模型才能设为主模型")
                    conn.execute(
                        """
                        UPDATE model_versions
                        SET is_default = FALSE, updated_at = %s
                        WHERE registry_scope = %s AND is_default = TRUE
                          AND NOT (model_id = %s AND version = %s)
                        """,
                        (now, scope, model_id, int(version)),
                    )
                    conn.execute(
                        """
                        UPDATE model_versions
                        SET is_default = TRUE, registry_scope = %s,
                            registry_note = CASE WHEN %s = '' THEN registry_note ELSE %s END,
                            updated_at = %s
                        WHERE model_id = %s AND version = %s
                        """,
                        (scope, clean_note, clean_note, now, model_id, int(version)),
                    )
                elif normalized_action == "clear_default":
                    conn.execute(
                        """
                        UPDATE model_versions
                        SET is_default = FALSE,
                            registry_note = CASE WHEN %s = '' THEN registry_note ELSE %s END,
                            updated_at = %s
                        WHERE model_id = %s AND version = %s
                        """,
                        (clean_note, clean_note, now, model_id, int(version)),
                    )
                elif normalized_action == "archive":
                    if bool(current.get("is_default")):
                        raise ModelResearchConflict("主模型不能直接归档；请先取消主模型或切换其他版本")
                    if current_state != "archived":
                        conn.execute(
                            """
                            UPDATE model_versions
                            SET state = 'archived', is_default = FALSE,
                                registry_scope = %s, registry_note = %s,
                                archived_at = %s, updated_at = %s
                            WHERE model_id = %s AND version = %s
                            """,
                            (scope, clean_note, now, now, model_id, int(version)),
                        )
                        conn.execute(
                            """
                            UPDATE model_inference_schedules
                            SET enabled = FALSE, updated_at = %s
                            WHERE model_id = %s AND model_version = %s
                            """,
                            (now, model_id, int(version)),
                        )
                        conn.execute(
                            """
                            UPDATE model_strategy_deployments
                            SET state = 'paused', updated_at = %s
                            WHERE model_id = %s AND model_version = %s
                              AND state = 'active'
                            """,
                            (now, model_id, int(version)),
                        )
                elif normalized_action == "restore":
                    if current_state == "archived":
                        conn.execute(
                            """
                            UPDATE model_versions
                            SET state = %s, is_default = FALSE,
                                registry_scope = %s, registry_note = %s,
                                archived_at = NULL, updated_at = %s
                            WHERE model_id = %s AND version = %s
                            """,
                            (
                                "validated" if validation_approved else "candidate",
                                scope, clean_note, now, model_id, int(version),
                            ),
                        )
                else:
                    conn.execute(
                        """
                        UPDATE model_versions
                        SET registry_scope = %s, registry_note = %s, updated_at = %s
                        WHERE model_id = %s AND version = %s
                        """,
                        (scope, clean_note, now, model_id, int(version)),
                    )
                self._event(
                    conn,
                    str(current["job_id"]),
                    "model.registry_updated",
                    stage="registry",
                    message=normalized_action,
                    payload={
                        "model_id": model_id,
                        "model_version": int(version),
                        "action": normalized_action,
                        "registry_scope": scope,
                        "validation_approved": bool(validation_approved),
                    },
                )
        return self.get_model(model_id, version)

    def delete_model(self, model_id: str, version: int) -> dict[str, Any]:
        """Permanently delete one unreferenced model version and its job metadata."""

        clean_model_id = _required_identifier(model_id, "model_id")
        clean_version = int(version)
        if clean_version <= 0:
            raise ModelResearchError("model_version必须是正整数")
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    SELECT versions.*, specs.spec_hash AS dataset_hash
                    FROM model_versions versions
                    JOIN model_dataset_specs specs USING(dataset_id)
                    WHERE versions.model_id = %s AND versions.version = %s
                    FOR UPDATE OF versions
                    """,
                    (clean_model_id, clean_version),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("模型版本不存在")
                current = dict(row)
                if bool(current.get("is_default")):
                    raise ModelResearchConflict("主模型不能删除；请先取消主模型或切换其他版本")

                deployments = conn.execute(
                    """
                    SELECT deployment_id, mode, state
                    FROM model_strategy_deployments
                    WHERE model_id = %s AND model_version = %s
                    ORDER BY created_at
                    """,
                    (clean_model_id, clean_version),
                ).fetchall()
                if deployments:
                    raise ModelResearchConflict(
                        "模型存在模拟盘或策略部署，必须先删除关联策略："
                        + "、".join(str(item["deployment_id"]) for item in deployments)
                    )

                architectures = conn.execute(
                    """
                    SELECT architectures.architecture_id, architectures.name,
                           architectures.state
                    FROM model_architecture_engines engines
                    JOIN model_architectures architectures
                      USING(architecture_id)
                    WHERE engines.model_id = %s AND engines.model_version = %s
                    ORDER BY architectures.created_at
                    """,
                    (clean_model_id, clean_version),
                ).fetchall()
                if architectures:
                    raise ModelResearchConflict(
                        "模型仍被模型架构引用："
                        + "、".join(
                            str(item.get("name") or item["architecture_id"])
                            for item in architectures
                        )
                    )

                active_jobs = conn.execute(
                    """
                    SELECT job_id, kind, status
                    FROM model_jobs
                    WHERE model_id = %s AND model_version = %s
                      AND job_id <> %s
                      AND status NOT IN ('succeeded', 'failed', 'canceled')
                    ORDER BY requested_at
                    """,
                    (clean_model_id, clean_version, str(current["job_id"])),
                ).fetchall()
                if active_jobs:
                    raise ModelResearchConflict(
                        "模型仍有运行中的推理或研究任务："
                        + "、".join(str(item["job_id"]) for item in active_jobs)
                    )

                candidates = conn.execute(
                    """
                    SELECT versions.model_id, versions.version, versions.name,
                           versions.manifest_json, jobs.config_json
                    FROM model_versions versions
                    JOIN model_jobs jobs USING(job_id)
                    WHERE NOT (
                        versions.model_id = %s AND versions.version = %s
                    )
                    ORDER BY versions.created_at DESC
                    """,
                    (clean_model_id, clean_version),
                ).fetchall()
                dependents = [
                    dict(item)
                    for item in candidates
                    if _model_payload_references(
                        item,
                        model_id=clean_model_id,
                        model_version=clean_version,
                    )
                ]
                if dependents:
                    raise ModelResearchConflict(
                        "模型仍被其他冻结模型引用："
                        + "、".join(
                            f"{item.get('name') or item['model_id']} · v{item['version']}"
                            for item in dependents
                        )
                    )

                conn.execute(
                    "DELETE FROM model_versions WHERE model_id = %s AND version = %s",
                    (clean_model_id, clean_version),
                )
                conn.execute(
                    "DELETE FROM model_jobs WHERE job_id = %s",
                    (str(current["job_id"]),),
                )
                dataset_deleted = bool(conn.execute(
                    """
                    DELETE FROM model_dataset_specs specs
                    WHERE specs.dataset_id = %s
                      AND NOT EXISTS (
                          SELECT 1 FROM model_jobs jobs
                          WHERE jobs.dataset_id = specs.dataset_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM model_versions versions
                          WHERE versions.dataset_id = specs.dataset_id
                      )
                    RETURNING dataset_id
                    """,
                    (str(current["dataset_id"]),),
                ).fetchone())
        return {
            "model_id": clean_model_id,
            "version": clean_version,
            "name": str(current.get("name") or clean_model_id),
            "job_id": str(current["job_id"]),
            "dataset_id": str(current["dataset_id"]),
            "dataset_hash": str(current.get("dataset_hash") or ""),
            "dataset_deleted": dataset_deleted,
        }

    def create_model_architecture(
        self, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        engines = payload.get("engines")
        if not isinstance(engines, list):
            raise ModelResearchError("模型架构engines必须是数组")
        models = [
            self.get_model(
                str((item or {}).get("model_id") or ""),
                int((item or {}).get("model_version") or (item or {}).get("version") or 0),
            )
            for item in engines
        ]
        spec = _architecture_spec(payload, models)
        architecture_id = _clean_identifier(
            str(payload.get("architecture_id") or ""),
            default=f"architecture_{uuid4().hex[:16]}",
        )
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO model_architectures(
                        architecture_id, name, description, state, revision,
                        universe_id, merge_method, top_n, rebalance_every,
                        config_json, created_at, updated_at
                    ) VALUES (%s, %s, %s, 'draft', 1, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        architecture_id, spec["name"], spec["description"],
                        spec["universe_id"], spec["merge_method"], spec["top_n"],
                        spec["rebalance_every"], Jsonb(spec), now, now,
                    ),
                )
                self._replace_architecture_engines(
                    conn, architecture_id, spec["engines"], now,
                )
        return self.get_model_architecture(architecture_id)

    def create_research_template(
        self, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        spec = _research_template_spec(payload)
        template_id = _clean_identifier(
            str(payload.get("template_id") or ""),
            default=f"research_template_{uuid4().hex[:16]}",
        )
        config_hash = sha256(
            _canonical_json(spec["training"]).encode("utf-8")
        ).hexdigest()
        now = _utcnow()
        with self.database.connection() as conn:
            conn.execute(
                """
                INSERT INTO model_research_templates(
                    template_id, name, description, state, revision,
                    config_hash, config_json, created_at, updated_at
                ) VALUES (%s, %s, %s, 'active', 1, %s, %s, %s, %s)
                """,
                (
                    template_id, spec["name"], spec["description"],
                    config_hash, Jsonb(spec), now, now,
                ),
            )
        return self.get_research_template(template_id)

    def list_research_templates(
        self, *, state: str = "active", limit: int = 100,
    ) -> list[dict[str, Any]]:
        normalized_state = str(state or "active").strip().lower()
        if normalized_state not in {"active", "archived", "all"}:
            raise ModelResearchError("研究模板state只支持active、archived或all")
        conditions = []
        values: list[Any] = []
        if normalized_state != "all":
            conditions.append("state = %s")
            values.append(normalized_state)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(max(1, min(int(limit), 200)))
        with self.database.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT template_id
                FROM model_research_templates
                {where}
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                tuple(values),
            ).fetchall()
        return [
            self.get_research_template(str(row["template_id"]))
            for row in rows
        ]

    def get_research_template(self, template_id: str) -> dict[str, Any]:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM model_research_templates WHERE template_id = %s",
                (template_id,),
            ).fetchone()
        if not row:
            raise ModelResearchNotFound("研究模板不存在")
        config = dict(row.get("config_json") or {})
        return _json_ready_mapping({
            **config,
            "template_id": str(row["template_id"]),
            "name": str(row["name"]),
            "description": str(row["description"]),
            "state": str(row["state"]),
            "revision": int(row["revision"]),
            "config_hash": str(row["config_hash"]),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        })

    def update_research_template(
        self, template_id: str, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        spec = _research_template_spec(payload)
        expected_revision = int(payload.get("revision") or 0)
        if expected_revision <= 0:
            raise ModelResearchError("更新研究模板必须提供revision")
        config_hash = sha256(
            _canonical_json(spec["training"]).encode("utf-8")
        ).hexdigest()
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    SELECT state, revision
                    FROM model_research_templates
                    WHERE template_id = %s
                    FOR UPDATE
                    """,
                    (template_id,),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("研究模板不存在")
                if str(row["state"]) != "active":
                    raise ModelResearchConflict("已归档研究模板不能修改")
                if expected_revision != int(row["revision"]):
                    raise ModelResearchConflict("研究模板已被其他请求修改，请刷新后重试")
                conn.execute(
                    """
                    UPDATE model_research_templates
                    SET name = %s, description = %s, revision = revision + 1,
                        config_hash = %s, config_json = %s, updated_at = %s
                    WHERE template_id = %s
                    """,
                    (
                        spec["name"], spec["description"], config_hash,
                        Jsonb(spec), now, template_id,
                    ),
                )
        return self.get_research_template(template_id)

    def archive_research_template(self, template_id: str) -> dict[str, Any]:
        now = _utcnow()
        with self.database.connection() as conn:
            row = conn.execute(
                """
                UPDATE model_research_templates
                SET state = 'archived', revision = revision + 1, updated_at = %s
                WHERE template_id = %s AND state = 'active'
                RETURNING template_id
                """,
                (now, template_id),
            ).fetchone()
        if not row:
            raise ModelResearchNotFound("研究模板不存在或已归档")
        return self.get_research_template(template_id)

    def list_model_architectures(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connection() as conn:
            rows = conn.execute(
                """
                SELECT architecture_id
                FROM model_architectures
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [
            self.get_model_architecture(str(row["architecture_id"]))
            for row in rows
        ]

    def get_model_architecture(self, architecture_id: str) -> dict[str, Any]:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM model_architectures WHERE architecture_id = %s",
                (architecture_id,),
            ).fetchone()
        if not row:
            raise ModelResearchNotFound("模型架构不存在")
        config = dict(row.get("config_json") or {})
        current_models: list[dict[str, Any]] = []
        for engine in config.get("engines") or []:
            try:
                current_models.append(self.get_model(
                    str(engine.get("model_id") or ""),
                    int(engine.get("model_version") or 0),
                ))
            except ModelResearchNotFound:
                continue
        result = {
            **config,
            "architecture_id": str(row["architecture_id"]),
            "state": str(row["state"]),
            "revision": int(row["revision"]),
            "activated_at": row.get("activated_at"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
        result["readiness"] = _architecture_readiness(config, current_models)
        return _json_ready_mapping(result)

    def update_model_architecture(
        self, architecture_id: str, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        engines = payload.get("engines")
        if not isinstance(engines, list):
            raise ModelResearchError("模型架构engines必须是数组")
        models = [
            self.get_model(
                str((item or {}).get("model_id") or ""),
                int((item or {}).get("model_version") or (item or {}).get("version") or 0),
            )
            for item in engines
        ]
        spec = _architecture_spec(payload, models)
        expected_revision = int(payload.get("revision") or 0)
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM model_architectures WHERE architecture_id = %s FOR UPDATE",
                    (architecture_id,),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("模型架构不存在")
                if str(row["state"]) != "draft":
                    raise ModelResearchConflict("只有草稿架构可以修改；已激活版本必须新建架构")
                if expected_revision and expected_revision != int(row["revision"]):
                    raise ModelResearchConflict("模型架构已被其他请求修改，请刷新后重试")
                revision = int(row["revision"]) + 1
                conn.execute(
                    """
                    UPDATE model_architectures
                    SET name = %s, description = %s, revision = %s,
                        universe_id = %s, merge_method = %s, top_n = %s,
                        rebalance_every = %s, config_json = %s, updated_at = %s
                    WHERE architecture_id = %s
                    """,
                    (
                        spec["name"], spec["description"], revision,
                        spec["universe_id"], spec["merge_method"], spec["top_n"],
                        spec["rebalance_every"], Jsonb(spec), now, architecture_id,
                    ),
                )
                conn.execute(
                    "DELETE FROM model_architecture_engines WHERE architecture_id = %s",
                    (architecture_id,),
                )
                self._replace_architecture_engines(
                    conn, architecture_id, spec["engines"], now,
                )
        return self.get_model_architecture(architecture_id)

    def activate_model_architecture(self, architecture_id: str) -> dict[str, Any]:
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT * FROM model_architectures WHERE architecture_id = %s FOR UPDATE",
                    (architecture_id,),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("模型架构不存在")
                if str(row["state"]) != "draft":
                    raise ModelResearchConflict("只有草稿架构可以激活")
                config = dict(row.get("config_json") or {})
                engine_rows = conn.execute(
                    """
                    SELECT engines.model_id, engines.model_version,
                           versions.state, versions.prediction_json
                    FROM model_architecture_engines engines
                    JOIN model_versions versions
                      ON versions.model_id = engines.model_id
                     AND versions.version = engines.model_version
                    WHERE engines.architecture_id = %s AND engines.enabled = TRUE
                    ORDER BY engines.priority, engines.engine_key
                    """,
                    (architecture_id,),
                ).fetchall()
                readiness = _architecture_readiness(config, [dict(item) for item in engine_rows])
                if readiness["ready"] is not True:
                    failed = [
                        str(item["label"])
                        for item in readiness["checks"] if item["passed"] is not True
                    ]
                    raise ModelResearchConflict("模型架构尚不可激活：" + "、".join(failed))
                conn.execute(
                    """
                    UPDATE model_architectures
                    SET state = 'active', activated_at = %s, updated_at = %s
                    WHERE architecture_id = %s
                    """,
                    (now, now, architecture_id),
                )
        return self.get_model_architecture(architecture_id)

    def archive_model_architecture(self, architecture_id: str) -> dict[str, Any]:
        now = _utcnow()
        with self.database.connection() as conn:
            row = conn.execute(
                """
                UPDATE model_architectures
                SET state = 'archived', updated_at = %s
                WHERE architecture_id = %s AND state <> 'archived'
                RETURNING architecture_id
                """,
                (now, architecture_id),
            ).fetchone()
        if not row:
            raise ModelResearchNotFound("模型架构不存在或已归档")
        return self.get_model_architecture(architecture_id)

    @staticmethod
    def _replace_architecture_engines(
        conn: Any, architecture_id: str, engines: list[Mapping[str, Any]],
        now: datetime,
    ) -> None:
        for engine in engines:
            conn.execute(
                """
                INSERT INTO model_architecture_engines(
                    architecture_id, engine_key, display_name, role,
                    model_id, model_version, priority, enabled, weight,
                    score_threshold, top_n, snapshot_json, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    architecture_id, engine["engine_key"], engine["display_name"],
                    engine["role"], engine["model_id"], engine["model_version"],
                    engine["priority"], engine["enabled"], engine["weight"],
                    engine["score_threshold"], engine["top_n"], Jsonb(dict(engine)),
                    now, now,
                ),
            )

    def record_validation_result(
        self,
        model_id: str,
        version: int,
        factor_backtest_job_id: str,
        *,
        approved: bool,
        validation: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    UPDATE model_versions
                    SET state = CASE WHEN state = 'archived' THEN state ELSE %s END,
                        manifest_json = jsonb_set(
                            COALESCE(manifest_json, '{}'::jsonb),
                            '{validation}', %s, TRUE
                        ),
                        updated_at = %s
                    WHERE model_id = %s AND version = %s
                    RETURNING *
                    """,
                    (
                        "validated" if approved else "candidate",
                        Jsonb(dict(validation)), now, model_id, int(version),
                    ),
                ).fetchone()
                if not row:
                    raise ModelResearchNotFound("模型版本不存在")
                conn.execute(
                    """
                    UPDATE model_jobs SET factor_backtest_job_id = %s, updated_at = %s
                    WHERE job_id = %s
                    """,
                    (factor_backtest_job_id, now, row["job_id"]),
                )
                if approved and str(row.get("state") or "") != "archived":
                    conn.execute(
                        """
                        INSERT INTO model_inference_schedules(
                            model_id, model_version, enabled, created_at, updated_at
                        ) VALUES (%s, %s, TRUE, %s, %s)
                        ON CONFLICT(model_id, model_version) DO UPDATE SET
                            enabled = TRUE, updated_at = EXCLUDED.updated_at
                        """,
                        (model_id, int(version), now, now),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE model_inference_schedules
                        SET enabled = FALSE, updated_at = %s
                        WHERE model_id = %s AND model_version = %s
                        """,
                        (now, model_id, int(version)),
                    )
        return dict(row)

    def mark_validated(
        self,
        model_id: str,
        version: int,
        factor_backtest_job_id: str,
        *,
        validation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.record_validation_result(
            model_id,
            version,
            factor_backtest_job_id,
            approved=True,
            validation=validation or {
                "policy": "manual",
                "passed": True,
                "manual_override": True,
                "backtest_job_id": factor_backtest_job_id,
            },
        )

    def list_inference_schedules(self) -> list[dict[str, Any]]:
        with self.database.connection() as conn:
            rows = conn.execute(
                """
                SELECT schedules.*, versions.name, versions.model_kind, versions.state,
                       versions.is_default,
                       versions.metrics_json, versions.prediction_json,
                       versions.manifest_json,
                       specs.spec_hash AS dataset_hash,
                       specs.spec_json AS dataset_spec
                FROM model_inference_schedules schedules
                JOIN model_versions versions
                  ON versions.model_id = schedules.model_id
                 AND versions.version = schedules.model_version
                JOIN model_dataset_specs specs USING(dataset_id)
                ORDER BY schedules.updated_at DESC
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["run_after_local"] = str(item.get("run_after_local") or "16:30")[:5]
            result.append(item)
        return result

    def update_inference_schedule(
        self, model_id: str, version: int, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        enabled = bool(payload.get("enabled", True))
        run_after = str(payload.get("run_after_local") or "16:30")[:5]
        try:
            datetime.strptime(run_after, "%H:%M")
        except ValueError as exc:
            raise ModelResearchError("run_after_local必须是HH:MM") from exc
        max_catchup = max(1, min(int(payload.get("max_catchup_days") or 20), 250))
        now = _utcnow()
        with self.database.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO model_inference_schedules(
                    model_id, model_version, enabled, run_after_local,
                    max_catchup_days, created_at, updated_at
                ) VALUES (%s, %s, %s, %s::time, %s, %s, %s)
                ON CONFLICT(model_id, model_version) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    run_after_local = EXCLUDED.run_after_local,
                    max_catchup_days = EXCLUDED.max_catchup_days,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (model_id, int(version), enabled, run_after, max_catchup, now, now),
            ).fetchone()
        if not row:
            raise ModelResearchNotFound("模型版本不存在")
        schedule = dict(row)
        schedule["run_after_local"] = str(schedule.get("run_after_local") or "16:30")[:5]
        return schedule

    def record_inference_schedule_tick(
        self, model_id: str, version: int, *, trade_date: str = "", error: str = "",
    ) -> None:
        with self.database.connection() as conn:
            conn.execute(
                """
                UPDATE model_inference_schedules
                SET last_checked_at = %s,
                    last_submitted_trade_date = CASE WHEN %s = '' THEN last_submitted_trade_date ELSE %s::date END,
                    last_error = %s, updated_at = %s
                WHERE model_id = %s AND model_version = %s
                """,
                (_utcnow(), trade_date, trade_date or None, str(error)[:2000], _utcnow(), model_id, int(version)),
            )

    def record_strategy_deployment(
        self, model_id: str, version: int, *, mode: str,
        snapshot: Mapping[str, Any], state: str = "active",
    ) -> dict[str, Any]:
        deployment_id = f"model_deploy_{sha256(f'{model_id}:{version}:{mode}'.encode()).hexdigest()[:20]}"
        with self.database.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO model_strategy_deployments(
                    deployment_id, model_id, model_version, mode, state,
                    top_n, rebalance_every, strategy_snapshot_json,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(model_id, model_version, mode) DO UPDATE SET
                    state = EXCLUDED.state,
                    strategy_snapshot_json = EXCLUDED.strategy_snapshot_json,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    deployment_id, model_id, int(version), mode, state,
                    int(snapshot.get("top_n") or 20), int(snapshot.get("rebalance_every") or 5),
                    Jsonb(dict(snapshot)), _utcnow(), _utcnow(),
                ),
            ).fetchone()
        return dict(row)

    def get_strategy_deployment(
        self, model_id: str, version: int, *, mode: str,
    ) -> dict[str, Any] | None:
        with self.database.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM model_strategy_deployments
                WHERE model_id = %s AND model_version = %s AND mode = %s
                """,
                (model_id, int(version), mode),
            ).fetchone()
        return dict(row) if row else None

    def record_artifact(
        self, *, job_id: str, artifact_kind: str, file_name: str,
        relative_path: str, digest: str, size_bytes: int, dataset_hash: str = "",
    ) -> dict[str, Any]:
        job = self.get_job(job_id)
        artifact_id = f"artifact_{sha256(f'{job_id}:{artifact_kind}:{file_name}'.encode()).hexdigest()[:24]}"
        with self.database.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO model_artifacts(
                    artifact_id, job_id, model_id, model_version, artifact_kind,
                    file_name, relative_path, sha256, size_bytes, dataset_hash
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(job_id, artifact_kind, file_name) DO UPDATE SET
                    relative_path = EXCLUDED.relative_path,
                    sha256 = EXCLUDED.sha256,
                    size_bytes = EXCLUDED.size_bytes,
                    dataset_hash = EXCLUDED.dataset_hash
                RETURNING *
                """,
                (
                    artifact_id, job_id, job["model_id"], job.get("model_version"),
                    artifact_kind, file_name, relative_path, digest, int(size_bytes),
                    dataset_hash,
                ),
            ).fetchone()
        return dict(row)

    def list_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        self.get_job(job_id)
        with self.database.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM model_artifacts WHERE job_id = %s ORDER BY created_at",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM model_artifacts WHERE artifact_id = %s",
                (artifact_id,),
            ).fetchone()
        if not row:
            raise ModelResearchNotFound("模型产物不存在")
        return dict(row)

    @staticmethod
    def _assert_lease(row: Mapping[str, Any], lease_token: str) -> None:
        if str(row.get("status")) not in ACTIVE_STATUSES:
            raise ModelResearchConflict("任务不是可完成状态")
        if (
            str(row.get("lease_owner")) != "alpha-factor-service"
            or str(row.get("lease_token")) != lease_token
        ):
            raise ModelResearchConflict("任务租约不属于模型研究调度服务")

    def _recover_expired(self, conn: Any, now: datetime) -> None:
        rows = conn.execute(
            """
            SELECT * FROM model_jobs
            WHERE status IN ('leased', 'running', 'uploading')
              AND lease_expires_at < %s
            FOR UPDATE SKIP LOCKED
            """,
            (now,),
        ).fetchall()
        for row in rows:
            exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
            status = "failed" if exhausted else "queued"
            conn.execute(
                """
                UPDATE model_jobs
                SET status = %s, lease_owner = '', lease_token = '',
                    lease_expires_at = NULL,
                    error_message = '调度服务租约过期',
                    finished_at = CASE WHEN %s = 'failed' THEN %s ELSE NULL END,
                    updated_at = %s
                WHERE job_id = %s
                """,
                (status, status, now, now, row["job_id"]),
            )
            self._event(conn, str(row["job_id"]), f"job.{status}", stage=status, message="调度服务租约过期")

    @staticmethod
    def _event(
        conn: Any, job_id: str, event_type: str, *, stage: str = "",
        message: str = "", payload: Mapping[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO model_job_events(job_id, event_type, stage, message, payload_json)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (job_id, event_type, stage, message, Jsonb(dict(payload or {}))),
        )


def _horizon_search_values(search_source: Mapping[str, Any]) -> list[int]:
    values = search_source.get("horizons")
    if not isinstance(values, list) or not 2 <= len(values) <= 4:
        raise ModelResearchError("多周期研究必须选择2到4个标签周期")
    horizons: list[int] = []
    for value in values:
        try:
            horizon = int(value)
        except (TypeError, ValueError) as exc:
            raise ModelResearchError("标签周期只支持T+1、T+3、T+5或T+10") from exc
        if horizon not in {1, 3, 5, 10}:
            raise ModelResearchError("标签周期只支持T+1、T+3、T+5或T+10")
        if horizon not in horizons:
            horizons.append(horizon)
    if len(horizons) < 2:
        raise ModelResearchError("多周期研究至少需要两个不同标签周期")
    return sorted(horizons)


def _factor_ablation_trials(
    dataset_source: Mapping[str, Any], search_source: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Freeze one baseline and one single-factor removal per requested factor.

    The variants intentionally keep model parameters fixed. Selection remains
    validation-only; any enabled Walk-Forward report is a later eligibility gate,
    never the ranking signal for choosing an ablation winner.
    """
    dataset = _dataset_spec(dataset_source)
    factors = list(dataset.get("factors") or [])
    if len(factors) < 2:
        raise ModelResearchError("因子消融实验至少需要两个冻结因子")
    values = search_source.get("factor_ids")
    if not isinstance(values, list) or not 1 <= len(values) <= 8:
        raise ModelResearchError("因子消融实验必须选择1到8个待检验因子")
    available = {str(item.get("factor_id") or "") for item in factors}
    factor_ids: list[str] = []
    for value in values:
        factor_id = str(value or "").strip()
        if not factor_id or factor_id not in available:
            raise ModelResearchError(f"待消融因子不存在于冻结数据集: {factor_id or value}")
        if factor_id not in factor_ids:
            factor_ids.append(factor_id)
    if not factor_ids:
        raise ModelResearchError("因子消融实验至少需要一个不同的待检验因子")
    max_trials = int(search_source.get("max_trials") or 9)
    max_trials = max(2, min(max_trials, MAX_EXPERIMENT_TRIALS))
    if len(factor_ids) + 1 > max_trials:
        raise ModelResearchError(
            f"消融方案共{len(factor_ids) + 1}组，超过本次上限{max_trials}组"
        )
    trials = [{
        "dataset": dataset,
        "search_params": {"removed_factor_id": "__baseline__"},
    }]
    for factor_id in factor_ids:
        trials.append({
            "dataset": _dataset_spec({
                **dataset_source,
                "factors": [
                    item for item in factors
                    if str(item.get("factor_id") or "") != factor_id
                ],
            }),
            "search_params": {"removed_factor_id": factor_id},
        })
    return trials


def _grid_search_trials(
    model_source: Mapping[str, Any], search_source: Mapping[str, Any],
) -> list[dict[str, Any]]:
    kind = str(model_source.get("kind") or "lightgbm").strip().lower()
    base = _model_spec(model_source)
    strategy = str(search_source.get("strategy") or "grid").strip().lower()
    if strategy != "grid":
        raise ModelResearchError("首版参数搜索只支持grid")
    parameters = search_source.get("parameters")
    if not isinstance(parameters, Mapping) or not parameters:
        raise ModelResearchError("参数实验至少需要一个搜索参数")
    unsupported = set(parameters) - SEARCHABLE_MODEL_PARAMS.get(kind, frozenset())
    if unsupported:
        raise ModelResearchError(
            f"{kind}不允许搜索参数: {', '.join(sorted(unsupported))}"
        )
    if len(parameters) > 4:
        raise ModelResearchError("一次参数实验最多搜索4个维度")
    keys = sorted(str(key) for key in parameters)
    value_sets: list[list[Any]] = []
    for key in keys:
        values = parameters[key]
        if not isinstance(values, list) or not 1 <= len(values) <= 8:
            raise ModelResearchError(f"搜索参数{key}必须包含1到8个候选值")
        unique: list[Any] = []
        fingerprints: set[str] = set()
        for value in values:
            fingerprint = _canonical_json(value)
            if fingerprint not in fingerprints:
                fingerprints.add(fingerprint)
                unique.append(value)
        value_sets.append(unique)
    trial_count = 1
    for values in value_sets:
        trial_count *= len(values)
    max_trials = int(search_source.get("max_trials") or MAX_EXPERIMENT_TRIALS)
    max_trials = max(1, min(max_trials, MAX_EXPERIMENT_TRIALS))
    if trial_count > max_trials:
        raise ModelResearchError(
            f"参数组合共{trial_count}组，超过本次上限{max_trials}组"
        )
    trials: list[dict[str, Any]] = []
    for values in product(*value_sets):
        overrides = dict(zip(keys, values, strict=True))
        normalized = _model_spec({
            "kind": kind,
            "params": {**base["params"], **overrides},
        })
        trials.append(dict(normalized["params"]))
    return trials


def _experiment_ref(source: Mapping[str, Any]) -> dict[str, Any]:
    if not source:
        return {}
    experiment_id = _required_identifier(
        str(source.get("experiment_id") or ""), "experiment_id",
    )
    trial_index = int(source.get("trial_index") or 0)
    trial_count = int(source.get("trial_count") or 0)
    if not 1 <= trial_index <= trial_count <= MAX_EXPERIMENT_TRIALS:
        raise ModelResearchError("参数实验试验序号无效")
    search_params = source.get("search_params")
    if not isinstance(search_params, Mapping) or not search_params:
        raise ModelResearchError("参数实验缺少search_params")
    strategy = str(source.get("strategy") or "grid").strip().lower()
    if strategy not in {"grid", "horizon_grid", "factor_ablation", "model_ensemble"}:
        raise ModelResearchError(
            "实验策略只支持grid、horizon_grid、factor_ablation或model_ensemble"
        )
    parent_experiment_id = str(source.get("parent_experiment_id") or "").strip()
    parent_job_id = str(source.get("parent_job_id") or "").strip()
    if parent_experiment_id:
        parent_experiment_id = _required_identifier(
            parent_experiment_id, "parent_experiment_id",
        )
    if parent_job_id:
        parent_job_id = _required_identifier(parent_job_id, "parent_job_id")
    iteration = int(source.get("iteration") or (2 if parent_experiment_id else 1))
    if not 1 <= iteration <= MAX_EXPERIMENT_ITERATIONS:
        raise ModelResearchError(
            f"实验迭代轮次必须在1到{MAX_EXPERIMENT_ITERATIONS}之间"
        )
    lineage_prior_trial_count = int(source.get("lineage_prior_trial_count") or 0)
    lineage_trial_budget = int(
        source.get("lineage_trial_budget") or MAX_LINEAGE_TRIALS
    )
    lineage_iteration_budget = int(
        source.get("lineage_iteration_budget") or MAX_EXPERIMENT_ITERATIONS
    )
    if not 0 <= lineage_prior_trial_count <= MAX_LINEAGE_TRIALS:
        raise ModelResearchError("实验谱系历史试验数量无效")
    if lineage_trial_budget != MAX_LINEAGE_TRIALS:
        raise ModelResearchError("实验谱系试验预算无效")
    if lineage_iteration_budget != MAX_EXPERIMENT_ITERATIONS:
        raise ModelResearchError("实验谱系轮次预算无效")
    return {
        "experiment_id": experiment_id,
        "title": str(source.get("title") or "参数实验")[:160],
        "strategy": strategy,
        "parent_experiment_id": parent_experiment_id,
        "parent_job_id": parent_job_id,
        "iteration": iteration,
        "lineage_prior_trial_count": lineage_prior_trial_count,
        "lineage_trial_budget": lineage_trial_budget,
        "lineage_iteration_budget": lineage_iteration_budget,
        "trial_index": trial_index,
        "trial_count": trial_count,
        "search_params": dict(search_params),
        "auto_dispatch": source.get("auto_dispatch") is True,
    }


def _experiment_summary(
    experiment_id: str, jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    first_experiment = dict(
        (jobs[0].get("config_json") or {}).get("experiment") or {}
    ) if jobs else {}
    first_job = jobs[0] if jobs else {}
    first_dataset = dict(first_job.get("dataset_spec") or {})
    search_parameters = sorted({
        str(key)
        for job in jobs
        for key in dict(
            ((job.get("config_json") or {}).get("experiment") or {}).get(
                "search_params"
            ) or {}
        )
    })
    completed_count = sum(
        count for status, count in statuses.items() if status in TERMINAL_STATUSES
    )
    strategy = str(first_experiment.get("strategy") or "grid")
    complete = bool(jobs) and completed_count == len(jobs)
    selection = (
        select_model_trial(jobs, complete=complete)
        if strategy == "model_ensemble"
        else select_parameter_trial(jobs, complete=complete)
    )
    if strategy == "horizon_grid":
        selection = {
            **selection,
            "policy": "alphablocks.horizon-selection.v1",
            "selection_unit": "label_horizon_trading_days",
        }
    elif strategy == "factor_ablation":
        selection = {
            **selection,
            "policy": "alphablocks.factor-ablation-selection.v1",
            "selection_unit": "removed_factor_id",
        }
    elif strategy == "model_ensemble":
        selection = {
            **selection,
            "selection_unit": "model_kind",
        }
    dataset_hashes = sorted({
        str(job.get("dataset_hash") or "") for job in jobs
        if str(job.get("dataset_hash") or "")
    })
    label_horizons = sorted({
        int(dict(job.get("dataset_spec") or {}).get("label", {}).get(
            "horizon_trading_days"
        ) or 5)
        for job in jobs
    })
    lineage_prior_trial_count = int(
        first_experiment.get("lineage_prior_trial_count") or 0
    )
    lineage_trial_budget = int(
        first_experiment.get("lineage_trial_budget") or MAX_LINEAGE_TRIALS
    )
    lineage_iteration_budget = int(
        first_experiment.get("lineage_iteration_budget")
        or MAX_EXPERIMENT_ITERATIONS
    )
    lineage_trial_count = lineage_prior_trial_count + len(jobs)
    return {
        "experiment_id": experiment_id,
        "title": str(first_experiment.get("title") or "参数实验"),
        "strategy": strategy,
        "parent_experiment_id": str(
            first_experiment.get("parent_experiment_id") or ""
        ),
        "parent_job_id": str(first_experiment.get("parent_job_id") or ""),
        "iteration": int(first_experiment.get("iteration") or 1),
        "lineage_prior_trial_count": lineage_prior_trial_count,
        "lineage_trial_count": lineage_trial_count,
        "lineage_trial_budget": lineage_trial_budget,
        "lineage_trial_remaining": max(0, lineage_trial_budget - lineage_trial_count),
        "lineage_iteration_budget": lineage_iteration_budget,
        "can_iterate": (
            int(first_experiment.get("iteration") or 1) < lineage_iteration_budget
            and lineage_trial_count + 2 <= lineage_trial_budget
        ),
        "model_id": str(first_job.get("model_id") or ""),
        "model_kind": str(first_job.get("model_kind") or ""),
        "dataset_hash": dataset_hashes[0] if len(dataset_hashes) == 1 else "",
        "dataset_hashes": dataset_hashes,
        "dataset_count": len(dataset_hashes),
        "shared_dataset": len(dataset_hashes) == 1,
        "label_horizons": label_horizons,
        "date_start": str(first_dataset.get("date_start") or ""),
        "date_end": str(first_dataset.get("date_end") or ""),
        "factor_count": len(first_dataset.get("factors") or []),
        "search_parameters": search_parameters,
        "created_at": min(
            (str(job.get("requested_at") or "") for job in jobs), default="",
        ),
        "updated_at": max(
            (str(job.get("updated_at") or "") for job in jobs), default="",
        ),
        "trial_count": len(jobs),
        "statuses": statuses,
        "completed_count": completed_count,
        "selection": selection,
        "comparison": _experiment_comparison(jobs, selection),
        "jobs": jobs,
    }


def _experiment_comparison(
    jobs: list[dict[str, Any]], selection: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a stable experiment comparison contract for the research UI.

    Validation metrics are always visible because they drive parameter selection.
    Test metrics remain hidden until every trial reaches a terminal state, preventing
    users from informally choosing parameters by repeatedly looking at the test set.
    """
    complete = bool(selection.get("complete"))
    assessments = {
        str(item.get("job_id") or ""): dict(item)
        for item in selection.get("trial_assessments") or []
    }
    rows = []
    for job in sorted(
        jobs,
        key=lambda item: int(
            ((item.get("config_json") or {}).get("experiment") or {}).get(
                "trial_index"
            ) or 0
        ),
    ):
        experiment = dict(
            (job.get("config_json") or {}).get("experiment") or {}
        )
        metrics = dict((job.get("result_json") or {}).get("metrics") or {})
        validation = _comparison_metric_values(metrics.get("validation") or {})
        test = _comparison_metric_values(metrics) if complete else None
        job_id = str(job.get("job_id") or "")
        assessment = assessments.get(job_id, {})
        rows.append({
            "job_id": job_id,
            "model_id": str(job.get("model_id") or ""),
            "model_version": int(job.get("model_version") or 0),
            "trial_index": int(experiment.get("trial_index") or 0),
            "status": str(job.get("status") or "unknown"),
            "search_params": dict(experiment.get("search_params") or {}),
            "dataset_hash": str(job.get("dataset_hash") or ""),
            "label_horizon_trading_days": int(
                dict(job.get("dataset_spec") or {}).get("label", {}).get(
                    "horizon_trading_days"
                ) or 5
            ),
            "validation": validation,
            "test": test,
            "gate_passed": assessment.get("passed") is True,
            "gate_checks": list(assessment.get("checks") or []),
            "failed_checks": list(assessment.get("failed_checks") or []),
            "is_selected": job_id == str(selection.get("selected_job_id") or ""),
            "is_best_observed": job_id == str(
                selection.get("best_observed_job_id") or ""
            ),
        })
    summary = _experiment_metric_summary(rows)
    return {
        "schema_version": "alphablocks.experiment-comparison.v1",
        "selection_split": "validation",
        "selection_metric": str(selection.get("ranking_metric") or "validation.rank_ic"),
        "test_metrics_role": "report_only",
        "test_metrics_disclosed": complete,
        "summary": summary,
        "parameter_effects": _experiment_parameter_effects(
            rows, test_metrics_disclosed=complete,
        ),
        "trials": rows,
    }


def _comparison_metric_values(source: Mapping[str, Any]) -> dict[str, Any]:
    values = dict(source or {})
    return {
        "days": values.get("days", values.get("test_days")),
        "rows": values.get("rows", values.get("test_rows")),
        "ic": values.get("ic"),
        "rank_ic": values.get("rank_ic"),
        "ic_ir": values.get("ic_ir"),
        "rmse": values.get("rmse"),
    }


def _experiment_metric_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    validation_rank_ic = [
        value for row in rows
        if (value := _finite_number(
            dict(row.get("validation") or {}).get("rank_ic")
        )) is not None
    ]
    validation_ic_ir = [
        value for row in rows
        if (value := _finite_number(
            dict(row.get("validation") or {}).get("ic_ir")
        )) is not None
    ]
    qualified_count = sum(row.get("gate_passed") is True for row in rows)
    return {
        "trial_count": len(rows),
        "evaluated_count": len(validation_rank_ic),
        "qualified_count": qualified_count,
        "qualified_ratio": (
            qualified_count / len(rows) if rows else 0.0
        ),
        "validation_rank_ic": _number_distribution(validation_rank_ic),
        "validation_ic_ir": _number_distribution(validation_ic_ir),
    }


def _experiment_parameter_effects(
    rows: list[Mapping[str, Any]], *, test_metrics_disclosed: bool,
) -> list[dict[str, Any]]:
    parameter_names = sorted({
        str(name)
        for row in rows
        for name in dict(row.get("search_params") or {})
    })
    effects: list[dict[str, Any]] = []
    for parameter_name in parameter_names:
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            params = dict(row.get("search_params") or {})
            if parameter_name not in params:
                continue
            value = params[parameter_name]
            fingerprint = _canonical_json(value)
            group = groups.setdefault(fingerprint, {
                "value": value,
                "value_label": fingerprint,
                "trial_count": 0,
                "evaluated_count": 0,
                "qualified_count": 0,
                "validation_rank_ic_values": [],
                "validation_ic_ir_values": [],
                "test_rank_ic_values": [],
            })
            group["trial_count"] += 1
            validation = dict(row.get("validation") or {})
            rank_ic = _finite_number(validation.get("rank_ic"))
            ic_ir = _finite_number(validation.get("ic_ir"))
            if rank_ic is not None:
                group["evaluated_count"] += 1
                group["validation_rank_ic_values"].append(rank_ic)
            if ic_ir is not None:
                group["validation_ic_ir_values"].append(ic_ir)
            if row.get("gate_passed") is True:
                group["qualified_count"] += 1
            if test_metrics_disclosed:
                test_rank_ic = _finite_number(
                    dict(row.get("test") or {}).get("rank_ic")
                )
                if test_rank_ic is not None:
                    group["test_rank_ic_values"].append(test_rank_ic)

        values = []
        for fingerprint in sorted(groups):
            group = groups[fingerprint]
            validation_distribution = _number_distribution(
                group.pop("validation_rank_ic_values")
            )
            ic_ir_distribution = _number_distribution(
                group.pop("validation_ic_ir_values")
            )
            test_distribution = _number_distribution(
                group.pop("test_rank_ic_values")
            ) if test_metrics_disclosed else None
            values.append({
                **group,
                "qualified_ratio": (
                    group["qualified_count"] / group["trial_count"]
                    if group["trial_count"] else 0.0
                ),
                "validation_rank_ic": validation_distribution,
                "validation_ic_ir": ic_ir_distribution,
                "test_rank_ic": test_distribution,
            })
        eligible = [
            item for item in values
            if item["validation_rank_ic"]["mean"] is not None
        ]
        best = max(
            eligible,
            key=lambda item: item["validation_rank_ic"]["mean"],
            default=None,
        )
        for item in values:
            item["is_best_validation"] = (
                best is not None and item["value_label"] == best["value_label"]
            )
        means = [
            item["validation_rank_ic"]["mean"] for item in eligible
        ]
        effects.append({
            "parameter": parameter_name,
            "value_count": len(values),
            "best_value": best["value"] if best is not None else None,
            "best_value_label": best["value_label"] if best is not None else "",
            "validation_rank_ic_spread": (
                max(means) - min(means) if len(means) >= 2 else 0.0
            ),
            "values": values,
        })
    return effects


def _number_distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "std": None, "min": None, "max": None}
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return {
        "count": len(values),
        "mean": mean,
        "std": math.sqrt(variance),
        "min": min(values),
        "max": max(values),
    }


def _model_prediction_range(
    model: Mapping[str, Any],
) -> tuple[str, str] | None:
    prediction = dict(model.get("prediction_json") or {})
    starts = [
        str(value)[:10] for value in [prediction.get("date_start")]
        if value and len(str(value)) >= 10
    ]
    ends = [
        str(value)[:10] for value in [
            prediction.get("date_end"), prediction.get("latest_trade_date"),
            prediction.get("last_inference_trade_date"),
        ]
        if value and len(str(value)) >= 10
    ]
    if not starts or not ends:
        return None
    return min(starts), max(ends)


def _compact_walk_forward(manifest: Mapping[str, Any]) -> dict[str, Any]:
    source = dict(manifest.get("walk_forward") or {})
    if source.get("enabled") is not True:
        return {"enabled": False, "window_count": 0, "windows": []}
    windows: list[dict[str, Any]] = []
    for position, item in enumerate(source.get("windows") or [], start=1):
        window = dict(item or {})
        test = list((window.get("segments") or {}).get("test") or [])
        if len(test) != 2:
            continue
        metrics = dict(window.get("metrics") or {})
        windows.append({
            "window": int(window.get("window") or position),
            "test_start": str(test[0])[:10],
            "test_end": str(test[1])[:10],
            "rank_ic": _finite_number(metrics.get("rank_ic", metrics.get("ic"))),
            "ic_ir": _finite_number(metrics.get("ic_ir")),
            "test_days": int(metrics.get("test_days") or 0),
        })
    return {
        "enabled": True,
        "strategy": str(source.get("strategy") or "rolling"),
        "window_count": len(windows),
        "windows": windows,
        "stability_status": str((source.get("stability") or {}).get("status") or ""),
    }


def _architecture_walk_forward_contract(
    engines: list[Mapping[str, Any]],
) -> dict[str, Any]:
    enabled = [dict(item) for item in engines if item.get("enabled") is True]
    all_enabled = bool(enabled) and all(
        (item.get("walk_forward") or {}).get("enabled") is True
        for item in enabled
    )
    signatures = [
        tuple(
            (
                str(window.get("test_start") or ""),
                str(window.get("test_end") or ""),
            )
            for window in (item.get("walk_forward") or {}).get("windows") or []
        )
        for item in enabled
    ]
    aligned = (
        all_enabled and bool(signatures) and bool(signatures[0])
        and len(set(signatures)) == 1
    )
    if not all_enabled:
        reason = "至少一个启用引擎不是Walk-Forward模型"
    elif not aligned:
        reason = "启用引擎的Walk-Forward测试窗口不一致"
    else:
        reason = ""
    return {
        "eligible": aligned,
        "source_count": len(enabled),
        "window_count": len(signatures[0]) if aligned else 0,
        "strategy": str(
            ((enabled[0].get("walk_forward") or {}).get("strategy") or "rolling")
        ) if enabled else "rolling",
        "windows": [
            {"window": index, "test_start": start, "test_end": end}
            for index, (start, end) in enumerate(signatures[0], start=1)
        ] if aligned else [],
        "reason": reason,
        "policy": "alphablocks.architecture-walk-forward.v1",
    }


def _architecture_spec(
    payload: Mapping[str, Any], source_models: list[Mapping[str, Any]],
) -> dict[str, Any]:
    engine_payloads = payload.get("engines")
    if not isinstance(engine_payloads, list) or not 1 <= len(engine_payloads) <= 8:
        raise ModelResearchError("模型架构必须包含1到8个引擎槽位")
    if len(source_models) != len(engine_payloads):
        raise ModelResearchError("模型架构的模型版本信息不完整")
    name = str(payload.get("name") or "").strip()[:160]
    if not name:
        raise ModelResearchError("模型架构名称不能为空")
    merge_method = str(payload.get("merge_method") or "priority").strip().lower()
    if merge_method not in {"priority", "weighted_score", "union"}:
        raise ModelResearchError("架构合并方式只支持priority、weighted_score或union")
    pipeline_mode = str(payload.get("pipeline_mode") or "flat").strip().lower()
    if pipeline_mode not in {"flat", "hierarchical"}:
        raise ModelResearchError("架构流程只支持flat或hierarchical")
    top_n = int(payload.get("top_n") or 20)
    rebalance_every = int(payload.get("rebalance_every") or 5)
    if not 1 <= top_n <= 100:
        raise ModelResearchError("架构TopN必须在1到100之间")
    if not 1 <= rebalance_every <= 60:
        raise ModelResearchError("架构调仓周期必须在1到60个交易日之间")

    seen_keys: set[str] = set()
    seen_models: set[tuple[str, int]] = set()
    priorities: list[int] = []
    universe_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, (source, model) in enumerate(
        zip(engine_payloads, source_models, strict=True), start=1,
    ):
        if not isinstance(source, Mapping):
            raise ModelResearchError(f"第{index}个引擎槽位格式无效")
        model_id = str(model.get("model_id") or "").strip()
        model_version = int(model.get("version") or 0)
        expected = (
            str(source.get("model_id") or "").strip(),
            int(source.get("model_version") or source.get("version") or 0),
        )
        if (model_id, model_version) != expected:
            raise ModelResearchConflict("引擎引用的模型版本在读取过程中发生变化")
        model_key = (model_id, model_version)
        if model_key in seen_models:
            raise ModelResearchError("同一模型版本不能重复加入一个架构")
        seen_models.add(model_key)
        engine_key = _clean_identifier(
            str(source.get("engine_key") or ""), default=f"engine_{index}",
        )
        if engine_key in seen_keys:
            raise ModelResearchError("引擎标识不能重复")
        seen_keys.add(engine_key)
        priority = int(source.get("priority") or index)
        if not 1 <= priority <= 100:
            raise ModelResearchError("引擎优先级必须在1到100之间")
        priorities.append(priority)
        weight = _finite_number(source.get("weight", 1.0))
        if weight is None or weight <= 0:
            raise ModelResearchError("引擎权重必须大于0")
        threshold = _finite_number(source.get("score_threshold", 0.0))
        if threshold is None or not -1.0 <= threshold <= 1.0:
            raise ModelResearchError("引擎Score阈值必须在-1到1之间")
        engine_top_n = int(source.get("top_n") or top_n)
        if not 1 <= engine_top_n <= 100:
            raise ModelResearchError("引擎TopN必须在1到100之间")
        role = str(source.get("role") or "stock_selection").strip().lower()
        if not role or len(role) > 64:
            raise ModelResearchError(f"第{index}个引擎职责无效")
        dataset = dict(model.get("dataset_spec") or {})
        universe_id = str(dataset.get("universe_id") or "").strip()
        if not universe_id:
            raise ModelResearchError("模型版本缺少冻结股票池")
        universe_ids.add(universe_id)
        factors = list(dataset.get("factors") or [])
        research_target = str(
            dataset.get("research_target") or "stock_selection"
        ).strip().lower()
        expected_target = {
            "market_style": "market_style",
            "industry_rotation": "industry_rotation",
        }.get(role, "stock_selection")
        if research_target != expected_target:
            raise ModelResearchError(
                f"引擎{engine_key}职责{role}与模型训练目标{research_target}不匹配"
            )
        categories = sorted({
            str((factor or {}).get("category") or "custom") for factor in factors
        })
        manifest = dict(model.get("manifest_json") or {})
        prediction = dict(model.get("prediction_json") or {})
        prediction_range = _model_prediction_range(model)
        stage = {
            "market_style": "style_gate",
            "industry_rotation": "industry_gate",
            "risk_filter": "risk_gate",
        }.get(role, "stock_rank")
        normalized.append({
            "engine_key": engine_key,
            "display_name": str(
                source.get("display_name") or source.get("name")
                or model.get("name") or f"引擎{index}"
            ).strip()[:120],
            "role": role,
            "stage": stage,
            "model_id": model_id,
            "model_version": model_version,
            "model_name": str(model.get("name") or model_id),
            "model_kind": str(model.get("model_kind") or ""),
            "dataset_hash": str(model.get("dataset_hash") or ""),
            "universe_id": universe_id,
            "research_target": research_target,
            "prediction_scope": str(
                dataset.get("prediction_scope")
                or (
                    "market_style" if research_target == "market_style"
                    else "industry" if research_target == "industry_rotation"
                    else "stock"
                )
            ),
            "label_horizon_trading_days": int(
                (dataset.get("label") or {}).get("horizon_trading_days") or 5
            ),
            "factor_count": len(factors),
            "factor_ids": [str((factor or {}).get("factor_id") or "") for factor in factors],
            "factor_categories": categories,
            "architecture": dict(manifest.get("model_params") or {}),
            "walk_forward": _compact_walk_forward(manifest),
            "registry_state": str(model.get("state") or "candidate"),
            "prediction_rows": int(prediction.get("row_count") or 0),
            "prediction_date_start": prediction_range[0] if prediction_range else "",
            "prediction_date_end": prediction_range[1] if prediction_range else "",
            "priority": priority,
            "enabled": bool(source.get("enabled", True)),
            "weight": float(weight),
            "score_threshold": float(threshold),
            "top_n": engine_top_n,
        })
    if len(universe_ids) != 1:
        raise ModelResearchError("同一架构的模型必须使用相同冻结股票池")
    priority_engines = (
        normalized if pipeline_mode == "flat"
        else [item for item in normalized if item["stage"] == "stock_rank"]
    )
    merge_priorities = [item["priority"] for item in priority_engines]
    if (
        merge_method == "priority"
        and len(merge_priorities) != len(set(merge_priorities))
    ):
        raise ModelResearchError("优先级合并模式要求每个个股引擎使用不同优先级")
    enabled = [item for item in normalized if item["enabled"]]
    if pipeline_mode == "hierarchical":
        stage_counts = {
            stage: sum(item["stage"] == stage for item in enabled)
            for stage in ("style_gate", "industry_gate", "stock_rank")
        }
        if stage_counts["style_gate"] != 1:
            raise ModelResearchError("三级门控架构必须且只能启用1个市场风格引擎")
        if stage_counts["industry_gate"] < 1:
            raise ModelResearchError("三级门控架构至少需要1个行业轮动引擎")
        if stage_counts["stock_rank"] < 1:
            raise ModelResearchError("三级门控架构至少需要1个个股选股引擎")
    weighted_engines = [
        item for item in normalized
        if item["enabled"]
        and (pipeline_mode == "flat" or item["stage"] == "stock_rank")
    ]
    enabled_weights = sum(item["weight"] for item in weighted_engines)
    for item in normalized:
        item["normalized_weight"] = (
            item["weight"] / enabled_weights
            if item in weighted_engines and enabled_weights > 0 else 0.0
        )
    normalized.sort(key=lambda item: (item["priority"], item["engine_key"]))
    fingerprint = sha256(_canonical_json({
        "schema_version": "alphablocks.model-architecture.v2",
        "universe_id": next(iter(universe_ids)),
        "pipeline_mode": pipeline_mode,
        "merge_method": merge_method,
        "top_n": top_n,
        "rebalance_every": rebalance_every,
        "engines": [{
            key: item[key] for key in (
                "engine_key", "role", "stage", "model_id", "model_version", "dataset_hash",
                "priority", "enabled", "normalized_weight", "score_threshold", "top_n",
            )
        } for item in normalized],
    }).encode("utf-8")).hexdigest()
    return {
        "schema_version": "alphablocks.model-architecture.v2",
        "name": name,
        "description": str(payload.get("description") or "").strip()[:2000],
        "universe_id": next(iter(universe_ids)),
        "pipeline_mode": pipeline_mode,
        "merge_method": merge_method,
        "top_n": top_n,
        "rebalance_every": rebalance_every,
        "engine_count": len(normalized),
        "enabled_engine_count": sum(item["enabled"] for item in normalized),
        "engines": normalized,
        "walk_forward": _architecture_walk_forward_contract(normalized),
        "fingerprint": fingerprint,
        "execution_contract": {
            "signal_time": "trade_date_close",
            "execution_time": "next_trade_date_open",
            "score_field": "score",
            "score_range": [-1.0, 1.0],
            "engine_features": "locked_by_model_version",
            "runtime_status": "research_backtest_ready",
            "pipeline_mode": pipeline_mode,
            "stage_order": (
                ["style_gate", "industry_gate", "risk_gate", "stock_rank"]
                if pipeline_mode == "hierarchical" else ["parallel_stock_rank"]
            ),
            "gate_semantics": (
                "each_enabled_gate_must_cover_entity_and_score_at_or_above_threshold"
                if pipeline_mode == "hierarchical" else "not_applicable"
            ),
        },
    }


def _architecture_readiness(
    config: Mapping[str, Any], current_models: list[Mapping[str, Any]],
) -> dict[str, Any]:
    engines = [dict(item) for item in config.get("engines") or []]
    enabled = [item for item in engines if item.get("enabled") is True]
    current = {
        (str(item.get("model_id") or ""), int(item.get("version") or item.get("model_version") or 0)): item
        for item in current_models
    }
    references = [
        (str(item.get("model_id") or ""), int(item.get("model_version") or 0))
        for item in enabled
    ]
    present = [current.get(key) for key in references]
    validated_count = sum(
        item is not None and str(item.get("state") or "") == "validated"
        for item in present
    )
    prediction_count = sum(
        item is not None
        and int((item.get("prediction_json") or {}).get("row_count") or 0) > 0
        for item in present
    )
    ranges = [
        _model_prediction_range(item) if item is not None else None
        for item in present
    ]
    complete_ranges = [item for item in ranges if item is not None]
    common_start = (
        max(item[0] for item in complete_ranges)
        if len(complete_ranges) == len(enabled) and enabled else ""
    )
    common_end = (
        min(item[1] for item in complete_ranges)
        if len(complete_ranges) == len(enabled) and enabled else ""
    )
    common_range_ready = bool(
        enabled and common_start and common_end and common_start < common_end
    )
    pipeline_mode = str(config.get("pipeline_mode") or "flat")
    stage_counts = {
        stage: sum(str(item.get("stage") or "stock_rank") == stage for item in enabled)
        for stage in ("style_gate", "industry_gate", "stock_rank")
    }
    topology_ready = (
        pipeline_mode == "flat"
        or (
            pipeline_mode == "hierarchical"
            and stage_counts["style_gate"] == 1
            and stage_counts["industry_gate"] >= 1
            and stage_counts["stock_rank"] >= 1
        )
    )
    checks = [
        {
            "key": "enabled_engines", "label": "至少启用一个引擎",
            "actual": len(enabled), "operator": ">=", "threshold": 1,
            "passed": len(enabled) >= 1,
        },
        {
            "key": "model_versions_present", "label": "模型版本完整",
            "actual": sum(item is not None for item in present), "operator": "=",
            "threshold": len(enabled),
            "passed": bool(enabled) and all(item is not None for item in present),
        },
        {
            "key": "models_validated", "label": "启用模型均已验证",
            "actual": validated_count, "operator": "=", "threshold": len(enabled),
            "passed": bool(enabled) and validated_count == len(enabled),
        },
        {
            "key": "predictions_ready", "label": "启用模型均有样本外预测",
            "actual": prediction_count, "operator": "=", "threshold": len(enabled),
            "passed": bool(enabled) and prediction_count == len(enabled),
        },
        {
            "key": "common_prediction_range",
            "label": "样本外预测日期存在共同区间",
            "actual": (
                f"{common_start} 至 {common_end}"
                if common_range_ready else "无可回测共同区间"
            ),
            "operator": "overlap", "threshold": "至少2个交易日",
            "passed": common_range_ready,
        },
        {
            "key": "single_universe", "label": "冻结股票池一致",
            "actual": len({str(item.get("universe_id") or "") for item in enabled}),
            "operator": "=", "threshold": 1,
            "passed": bool(enabled) and len({
                str(item.get("universe_id") or "") for item in enabled
            }) == 1,
        },
        {
            "key": "pipeline_topology", "label": "决策链角色完整",
            "actual": (
                "平行融合"
                if pipeline_mode == "flat"
                else (
                    f"风格{stage_counts['style_gate']} / "
                    f"行业{stage_counts['industry_gate']} / "
                    f"个股{stage_counts['stock_rank']}"
                )
            ),
            "operator": "valid", "threshold": (
                "无角色约束" if pipeline_mode == "flat" else "1 / >=1 / >=1"
            ),
            "passed": topology_ready,
        },
    ]
    failed = [item for item in checks if item["passed"] is not True]
    research_check_keys = {
        "enabled_engines", "model_versions_present", "predictions_ready",
        "common_prediction_range", "single_universe", "pipeline_topology",
    }
    research_failed = [
        item for item in checks
        if item["key"] in research_check_keys and item["passed"] is not True
    ]
    research_ready = not research_failed
    activation_ready = not failed
    return {
        "policy": "alphablocks.model-architecture-activation.v1",
        "backtest_policy": "alphablocks.model-architecture-backtest.v1",
        "ready": activation_ready,
        "checks": checks,
        "failed_checks": [item["key"] for item in failed],
        "research_backtest_ready": research_ready,
        "research_failed_checks": [item["key"] for item in research_failed],
        "definition_only": False,
        "pipeline_mode": pipeline_mode,
        "stage_counts": stage_counts,
        "runtime_message": (
            "架构定义已就绪，可运行组合信号回测；仍未接入实盘"
            if activation_ready
            else (
                "候选模型可运行组合研究回测；全部源模型验证后才能激活"
                if research_ready
                else "请先完成未通过的数据、预测日期与股票池检查"
            )
        ),
    }


def _ensemble_spec(
    payload: Mapping[str, Any], source_models: list[Mapping[str, Any]],
) -> dict[str, Any]:
    source_payloads = payload.get("sources")
    if not isinstance(source_payloads, list) or not 2 <= len(source_payloads) <= 8:
        raise ModelResearchError("融合模型必须选择2到8个源模型版本")
    if len(source_models) != len(source_payloads):
        raise ModelResearchError("源模型信息不完整")
    seen: set[tuple[str, int]] = set()
    universe_ids: set[str] = set()
    label_fingerprints: set[str] = set()
    research_targets: set[str] = set()
    prediction_scopes: set[str] = set()
    factor_fingerprints: set[str] = set()
    model_kinds: set[str] = set()
    model_param_fingerprints: set[str] = set()
    source_horizons: set[int] = set()
    normalized_sources: list[dict[str, Any]] = []
    for index, (source_payload, model) in enumerate(
        zip(source_payloads, source_models, strict=True)
    ):
        if not isinstance(source_payload, Mapping):
            raise ModelResearchError(f"第{index + 1}个源模型格式无效")
        model_id = str(model.get("model_id") or "")
        model_version = int(model.get("version") or 0)
        expected_id = str(source_payload.get("model_id") or "")
        expected_version = int(
            source_payload.get("model_version") or source_payload.get("version") or 0
        )
        if (model_id, model_version) != (expected_id, expected_version):
            raise ModelResearchConflict("源模型版本在读取过程中发生变化")
        key = (model_id, model_version)
        if key in seen:
            raise ModelResearchError("不能重复选择同一个源模型版本")
        if str(model.get("model_kind") or "") == "ensemble":
            raise ModelResearchError("首版不支持嵌套融合模型")
        seen.add(key)
        dataset = dict(model.get("dataset_spec") or {})
        universe_ids.add(str(dataset.get("universe_id") or ""))
        label = dict(dataset.get("label") or {})
        label_fingerprints.add(_canonical_json(label))
        horizon = int(label.get("horizon_trading_days") or 5)
        source_horizons.add(horizon)
        research_targets.add(str(dataset.get("research_target") or "stock_selection"))
        prediction_scopes.add(str(dataset.get("prediction_scope") or "stock"))
        factor_fingerprints.add(_canonical_json([
            {
                "factor_id": str((factor or {}).get("factor_id") or ""),
                "factor_version": int((factor or {}).get("factor_version") or 0),
                "params_hash": str((factor or {}).get("params_hash") or ""),
            }
            for factor in dataset.get("factors") or []
        ]))
        model_kind = str(model.get("model_kind") or "")
        model_kinds.add(model_kind)
        job_config = dict(model.get("job_config_json") or model.get("config_json") or {})
        model_param_fingerprints.add(_canonical_json(
            dict((job_config.get("model") or {}).get("params") or {})
        ))
        validation = dict((model.get("metrics_json") or {}).get("validation") or {})
        normalized_sources.append({
            "model_id": model_id,
            "model_version": model_version,
            "name": str(model.get("name") or model_id),
            "model_kind": model_kind,
            "dataset_hash": str(model.get("dataset_hash") or ""),
            "label_horizon_trading_days": horizon,
            "validation": {
                "rank_ic": _finite_number(validation.get("rank_ic")),
                "ic_ir": _finite_number(
                    validation.get("ic_ir", validation.get("rank_ic_ir"))
                ),
                "days": int(validation.get("days") or 0),
            },
        })
    if len(universe_ids) != 1 or "" in universe_ids:
        raise ModelResearchError("源模型必须使用相同股票池")
    ensemble_mode = str(
        payload.get("ensemble_mode") or "same_horizon"
    ).strip().lower()
    if ensemble_mode not in {"same_horizon", "multi_horizon"}:
        raise ModelResearchError("融合模式只支持same_horizon或multi_horizon")
    if ensemble_mode == "same_horizon" and len(label_fingerprints) != 1:
        raise ModelResearchError("源模型必须使用相同标签定义和预测周期")
    if ensemble_mode == "multi_horizon":
        if len(source_horizons) < 2:
            raise ModelResearchError("多周期融合至少需要两个不同标签周期")
        if len(research_targets) != 1 or len(prediction_scopes) != 1:
            raise ModelResearchError("多周期融合必须使用相同训练目标和预测实体")
        if len(factor_fingerprints) != 1:
            raise ModelResearchError("多周期融合必须使用完全相同的冻结因子集合")
        if len(model_kinds) != 1 or len(model_param_fingerprints) != 1:
            raise ModelResearchError("多周期融合必须使用相同算法和模型参数")
    raw_evaluation_horizon = payload.get("evaluation_horizon_trading_days")
    if raw_evaluation_horizon is None:
        evaluation_horizon = 5 if 5 in source_horizons else min(source_horizons)
    else:
        try:
            evaluation_horizon = int(raw_evaluation_horizon)
        except (TypeError, ValueError) as exc:
            raise ModelResearchError("融合评价周期必须是源模型已冻结的标签周期") from exc
    if evaluation_horizon not in source_horizons:
        raise ModelResearchError("融合评价周期必须是源模型已冻结的标签周期")

    strategy = str(payload.get("weight_strategy") or "equal").strip().lower()
    if strategy == "icir":
        strategy = "validation_icir"
    if strategy not in {"equal", "validation_icir", "manual"}:
        raise ModelResearchError("权重策略只支持equal、validation_icir或manual")
    raw_weights: list[float]
    fallback_reason = ""
    if strategy == "equal":
        raw_weights = [1.0] * len(normalized_sources)
    elif strategy == "validation_icir":
        raw_weights = [
            max(float(item["validation"]["ic_ir"] or 0.0), 0.0)
            for item in normalized_sources
        ]
        if sum(raw_weights) <= 0:
            raw_weights = [1.0] * len(normalized_sources)
            fallback_reason = "所有源模型验证集ICIR均不为正，已回退为等权"
    else:
        manual = payload.get("manual_weights")
        manual = manual if isinstance(manual, Mapping) else {}
        raw_weights = []
        for source_payload, item in zip(
            source_payloads, normalized_sources, strict=True
        ):
            source_payload = source_payload if isinstance(source_payload, Mapping) else {}
            key = f"{item['model_id']}:{item['model_version']}"
            value = source_payload.get(
                "weight", manual.get(key, manual.get(item["model_id"])),
            )
            weight = _finite_number(value)
            if weight is None or weight <= 0:
                raise ModelResearchError("手动权重要求每个源模型权重大于0")
            raw_weights.append(weight)
    total = sum(raw_weights)
    if not math.isfinite(total) or total <= 0:
        raise ModelResearchError("融合权重合计必须大于0")
    for item, weight in zip(normalized_sources, raw_weights, strict=True):
        item["weight"] = weight / total

    fingerprint = sha256(_canonical_json({
        "schema_version": "alphablocks.model-ensemble.v1",
        "ensemble_mode": ensemble_mode,
        "evaluation_horizon_trading_days": evaluation_horizon,
        "fusion_method": "linear_score",
        "weight_strategy": strategy,
        "sources": [
            {
                "model_id": item["model_id"],
                "model_version": item["model_version"],
                "dataset_hash": item["dataset_hash"],
                "weight": item["weight"],
            }
            for item in normalized_sources
        ],
    }).encode("utf-8")).hexdigest()
    model_id = _clean_identifier(
        str(payload.get("model_id") or ""),
        default=f"ensemble_{fingerprint[:16]}",
    )
    if model_id in {item["model_id"] for item in normalized_sources}:
        raise ModelResearchError("融合模型ID不能与源模型ID相同")
    default_name = " + ".join(item["model_kind"] for item in normalized_sources)
    name = str(payload.get("name") or payload.get("display_name") or f"融合模型 · {default_name}").strip()[:160]
    dataset = _ensemble_dataset_spec(
        fingerprint, source_models, normalized_sources,
        ensemble_mode=ensemble_mode,
        evaluation_horizon=evaluation_horizon,
    )
    return {
        "schema_version": "alphablocks.ensemble-config.v1",
        "model_id": model_id,
        "name": name,
        "fusion_method": "linear_score",
        "ensemble_mode": ensemble_mode,
        "evaluation_horizon_trading_days": evaluation_horizon,
        "source_horizons": sorted(source_horizons),
        "weight_strategy": strategy,
        "weight_metric": "validation.ic_ir" if strategy == "validation_icir" else "",
        "weight_fallback_reason": fallback_reason,
        "source_count": len(normalized_sources),
        "sources": normalized_sources,
        "fingerprint": fingerprint,
        "dataset": dataset,
    }


def _ensemble_dataset_spec(
    fingerprint: str, source_models: list[Mapping[str, Any]],
    sources: list[Mapping[str, Any]],
    *, ensemble_mode: str = "same_horizon", evaluation_horizon: int = 5,
) -> dict[str, Any]:
    datasets = [dict(model.get("dataset_spec") or {}) for model in source_models]
    date_start = max(_iso_date(item.get("date_start"), "date_start") for item in datasets)
    date_end = min(_iso_date(item.get("date_end"), "date_end") for item in datasets)
    if date_start >= date_end:
        raise ModelResearchError("源模型训练范围没有共同区间")
    data_cutoff = max(
        _iso_datetime(item.get("data_cutoff"), "data_cutoff") for item in datasets
    )
    factors: list[dict[str, Any]] = []
    seen_factors: set[tuple[str, int, str]] = set()
    for dataset in datasets:
        for factor in dataset.get("factors") or []:
            item = dict(factor or {})
            key = (
                str(item.get("factor_id") or ""),
                int(item.get("factor_version") or 0),
                str(item.get("params_hash") or ""),
            )
            if key not in seen_factors:
                seen_factors.add(key)
                factors.append(item)
    first = next(
        (
            item for item in datasets
            if int(dict(item.get("label") or {}).get(
                "horizon_trading_days"
            ) or 5) == int(evaluation_horizon)
        ),
        datasets[0],
    )
    return {
        "name": f"模型融合预测数据集 · {fingerprint[:12]}",
        "universe_id": str(first.get("universe_id") or "csi500"),
        "index_code": str(first.get("index_code") or "000905.SH"),
        "date_start": date_start,
        "date_end": date_end,
        "data_cutoff": data_cutoff,
        "research_target": str(first.get("research_target") or "stock_selection"),
        "prediction_scope": str(first.get("prediction_scope") or "stock"),
        "factors": factors,
        "feature_field": "source_model_score",
        "label": dict(first.get("label") or {}),
        "split": dict(first.get("split") or {}),
        "minimum_factor_coverage": min(
            float(item.get("minimum_factor_coverage") or 0.8) for item in datasets
        ),
        "materialization": {
            "mode": (
                "multi_horizon_model_prediction_fusion"
                if ensemble_mode == "multi_horizon"
                else "model_prediction_fusion"
            ),
            "format": "clickhouse",
            "persist_factor_values": False,
        },
        "availability": {
            "source_predictions_required_for_every_entity": True,
            "feature_cutoff_at_lte_signal_close": True,
            "evaluation_horizon_frozen": True,
        },
        "ensemble_mode": ensemble_mode,
        "evaluation_horizon_trading_days": int(evaluation_horizon),
        "ensemble_sources": [
            {
                "model_id": item["model_id"],
                "model_version": item["model_version"],
                "dataset_hash": item["dataset_hash"],
                "weight": item["weight"],
            }
            for item in sources
        ],
    }


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _json_ready_mapping(source: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in source.items():
        if isinstance(value, datetime):
            result[str(key)] = value.isoformat()
        elif hasattr(value, "isoformat"):
            result[str(key)] = value.isoformat()
        else:
            result[str(key)] = value
    return result


def _research_origin_spec(
    source: Mapping[str, Any],
    *,
    source_type: str,
    source_id: str,
    source_job: Mapping[str, Any],
    source_model_id: str,
    source_model_version: int,
    dataset: Mapping[str, Any],
    model: Mapping[str, Any],
    walk_forward: Mapping[str, Any],
) -> dict[str, Any]:
    source_config = dict(source_job.get("config_json") or {})
    source_dataset = _dataset_spec(source_job.get("dataset_spec") or {})
    source_model = _model_spec(source_config.get("model") or {})
    source_walk_forward = _walk_forward_spec(
        source_config.get("walk_forward") or {}
    )
    comparisons = {
        "dataset": (dataset, source_dataset),
        "model": (model, source_model),
        "walk_forward": (walk_forward, source_walk_forward),
    }
    changed_sections = [
        name for name, (current, historical) in comparisons.items()
        if _canonical_json(current) != _canonical_json(historical)
    ]
    declared = source.get("declared_changes") or []
    if declared and not isinstance(declared, list):
        raise ModelResearchError("research_origin.declared_changes必须是数组")
    for value in declared:
        label = str(value or "").strip()[:80]
        if label and label not in changed_sections:
            changed_sections.append(label)
    requested_mode = str(
        source.get("requested_mode") or "derived"
    ).strip().lower()
    if requested_mode not in {"exact_replay", "derived"}:
        raise ModelResearchError(
            "research_origin.requested_mode只支持exact_replay或derived"
        )
    if requested_mode == "exact_replay" and changed_sections:
        raise ModelResearchConflict(
            "精确复现配置已发生变化：" + "、".join(changed_sections)
        )
    source_snapshot = {
        "dataset": source_dataset,
        "model": source_model,
        "walk_forward": source_walk_forward,
    }
    return {
        "schema_version": "alphablocks.research-origin.v1",
        "source_type": source_type,
        "source_id": source_id,
        "source_job_id": str(source_job.get("job_id") or ""),
        "source_model_id": source_model_id,
        "source_model_version": int(source_model_version),
        "requested_mode": requested_mode,
        "mode": "exact_replay" if requested_mode == "exact_replay" else "derived",
        "changed_sections": changed_sections,
        "source_dataset_hash": str(source_job.get("dataset_hash") or ""),
        "source_config_hash": sha256(
            _canonical_json(source_snapshot).encode("utf-8")
        ).hexdigest(),
    }


def _incremental_training_assessment(
    source_model: Mapping[str, Any],
    bundle: Mapping[str, Any] | None,
    *,
    dataset: Mapping[str, Any],
    model: Mapping[str, Any],
    walk_forward: Mapping[str, Any],
) -> dict[str, Any]:
    source_dataset = _dataset_spec(source_model.get("dataset_spec") or {})
    source_config = dict(source_model.get("job_config_json") or {})
    source_model_spec = _model_spec(source_config.get("model") or {})
    source_walk_forward = _walk_forward_spec(
        source_config.get("walk_forward") or {}
    )

    def factor_identity(spec: Mapping[str, Any]) -> list[tuple[str, int, str]]:
        return [
            (
                str(item.get("factor_id") or ""),
                int(item.get("factor_version") or 0),
                str(item.get("params_hash") or ""),
            )
            for item in spec.get("factors") or []
        ]

    def stable_model_params(spec: Mapping[str, Any]) -> dict[str, Any]:
        params = dict(spec.get("params") or {})
        # These control only how many new trees may be appended and when the
        # update stops. Every structural/objective parameter remains frozen.
        params.pop("n_estimators", None)
        params.pop("early_stopping_rounds", None)
        return params

    source_end = str(source_dataset.get("date_end") or "")
    candidate_end = str(dataset.get("date_end") or "")
    source_cutoff = str(source_dataset.get("data_cutoff") or "")
    candidate_cutoff = str(dataset.get("data_cutoff") or "")
    bundle_mapping = dict(bundle or {})
    bundle_ready = bool(
        bundle_mapping.get("artifact_id")
        and bundle_mapping.get("relative_path")
        and len(str(bundle_mapping.get("sha256") or "")) == 64
    )
    source_kind = str(source_model_spec.get("kind") or "")
    candidate_kind = str(model.get("kind") or "")
    label_keys = ("kind", "horizon_trading_days", "range")
    source_label = {
        key: (source_dataset.get("label") or {}).get(key) for key in label_keys
    }
    candidate_label = {
        key: (dataset.get("label") or {}).get(key) for key in label_keys
    }
    checks = [
        {
            "key": "source_state",
            "label": "来源模型版本仍可用于研究",
            "passed": str(source_model.get("state") or "candidate") != "archived",
        },
        {
            "key": "model_kind",
            "label": "首版增量续训仅支持LightGBM",
            "passed": source_kind == candidate_kind == "lightgbm",
        },
        {
            "key": "source_artifact",
            "label": "来源模型Bundle完整且哈希已登记",
            "passed": bundle_ready,
        },
        {
            "key": "feature_identity",
            "label": "因子顺序、版本和参数哈希完全一致",
            "passed": factor_identity(source_dataset) == factor_identity(dataset),
        },
        {
            "key": "target_contract",
            "label": "股票池、样本过滤、研究目标和标签周期完全一致",
            "passed": (
                source_dataset.get("universe_id") == dataset.get("universe_id")
                and source_dataset.get("index_code") == dataset.get("index_code")
                and source_dataset.get("sample_filters") == dataset.get("sample_filters")
                and source_dataset.get("research_target") == dataset.get("research_target")
                and source_dataset.get("prediction_scope") == dataset.get("prediction_scope")
                and source_label == candidate_label
            ),
        },
        {
            "key": "model_parameters",
            "label": "模型结构和目标参数未改变",
            "passed": (
                stable_model_params(source_model_spec)
                == stable_model_params(model)
            ),
        },
        {
            "key": "date_extension",
            "label": "起始日不变且结束日向后扩展",
            "passed": (
                source_dataset.get("date_start") == dataset.get("date_start")
                and bool(source_end)
                and candidate_end > source_end
            ),
        },
        {
            "key": "data_cutoff",
            "label": "数据截止时间晚于来源模型",
            "passed": bool(source_cutoff) and candidate_cutoff > source_cutoff,
        },
        {
            "key": "walk_forward",
            "label": "增量续训暂不与Walk-Forward嵌套",
            "passed": (
                source_walk_forward.get("enabled") is not True
                and walk_forward.get("enabled") is not True
            ),
        },
    ]
    failed = [item["key"] for item in checks if item["passed"] is not True]
    passed = not failed
    contract = {
        "schema_version": "alphablocks.incremental-training.v1",
        "mode": "lightgbm_append_trees_new_data_only",
        "source_model_id": str(source_model.get("model_id") or ""),
        "source_model_version": int(source_model.get("version") or 0),
        "source_job_id": str(source_model.get("job_id") or ""),
        "source_dataset_hash": str(source_model.get("dataset_hash") or ""),
        "source_date_end": source_end,
        "candidate_date_end": candidate_end,
        "minimum_new_trading_sessions": 60,
        "source_artifact": {
            "artifact_id": str(bundle_mapping.get("artifact_id") or ""),
            "relative_path": str(bundle_mapping.get("relative_path") or ""),
            "sha256": str(bundle_mapping.get("sha256") or ""),
            "file_name": str(bundle_mapping.get("file_name") or ""),
        },
        "allowed_parameter_changes": [
            "n_estimators", "early_stopping_rounds",
        ],
    }
    return {
        "status": "ready" if passed else "blocked",
        "passed": passed,
        "can_submit": passed,
        "failed_checks": failed,
        "checks": checks,
        "contract": contract,
    }


def _research_template_spec(source: Mapping[str, Any]) -> dict[str, Any]:
    name = str(source.get("name") or "").strip()[:120]
    if not name:
        raise ModelResearchError("研究模板名称不能为空")
    training_source = source.get("training")
    if not isinstance(training_source, Mapping):
        raise ModelResearchError("研究模板缺少training配置")
    dataset = _dataset_spec(training_source.get("dataset") or {})
    model = _model_spec(training_source.get("model") or {})
    walk_forward = _walk_forward_spec(training_source.get("walk_forward") or {})
    execution = _execution_spec(training_source.get("execution") or {})
    design_source = training_source.get("research_design") or {}
    if not isinstance(design_source, Mapping):
        raise ModelResearchError("research_design必须是对象")
    mode = str(design_source.get("mode") or "single").strip().lower()
    if mode not in {"single", "grid", "horizon_grid", "factor_ablation"}:
        raise ModelResearchError(
            "研究模板mode只支持single、grid、horizon_grid或factor_ablation"
        )
    search_source = design_source.get("search") or {}
    if not isinstance(search_source, Mapping):
        raise ModelResearchError("research_design.search必须是对象")
    if mode == "single":
        search: dict[str, Any] = {}
    elif mode == "grid":
        normalized_parameters = {
            str(key): list(values) if isinstance(values, list) else values
            for key, values in sorted(
                dict(search_source.get("parameters") or {}).items(),
                key=lambda item: str(item[0]),
            )
        }
        search = {
            "strategy": "grid",
            "parameters": normalized_parameters,
            "max_trials": max(
                1,
                min(
                    int(search_source.get("max_trials") or MAX_EXPERIMENT_TRIALS),
                    MAX_EXPERIMENT_TRIALS,
                ),
            ),
        }
        _grid_search_trials(model, search)
    elif mode == "horizon_grid":
        horizons = _horizon_search_values(search_source)
        search = {
            "strategy": "horizon_grid",
            "horizons": horizons,
            "max_trials": len(horizons),
        }
    else:
        trials = _factor_ablation_trials(dataset, search_source)
        factor_ids = [
            str(item["search_params"]["removed_factor_id"])
            for item in trials[1:]
        ]
        search = {
            "strategy": "factor_ablation",
            "factor_ids": factor_ids,
            "max_trials": len(factor_ids) + 1,
        }
    model_id = _clean_identifier(str(training_source.get("model_id") or ""))
    training = {
        "schema_version": "alphablocks.model-training-template.v1",
        "title": str(training_source.get("title") or name).strip()[:160],
        "model_id": model_id,
        "dataset": dataset,
        "model": model,
        "walk_forward": walk_forward,
        "execution": execution,
        "research_design": {"mode": mode, "search": search},
    }
    return {
        "schema_version": "alphablocks.research-template.v1",
        "name": name,
        "description": str(source.get("description") or "").strip()[:1000],
        "training": training,
    }


def _dataset_spec(source: Mapping[str, Any]) -> dict[str, Any]:
    factors = source.get("factors")
    if not isinstance(factors, list) or not factors:
        raise ModelResearchError("至少选择一个训练特征")
    if len(factors) > 100:
        raise ModelResearchError("一次最多选择100个训练特征")
    normalized_factors = []
    seen: set[tuple[str, int, str]] = set()
    for item in factors:
        item = item if isinstance(item, Mapping) else {}
        if is_entity_field_feature(item):
            try:
                normalized = normalize_entity_field_feature(item)
            except ValueError as exc:
                raise ModelResearchError(str(exc)) from exc
            key = (
                normalized["factor_id"],
                normalized["factor_version"],
                normalized["params_hash"],
            )
            if key not in seen:
                seen.add(key)
                normalized_factors.append(normalized)
            continue
        factor_id = str(item.get("factor_id") or "").strip()
        version = int(item.get("factor_version") or item.get("version") or 0)
        params_hash = str(item.get("params_hash") or "").strip()
        params = item.get("params")
        if not factor_id or version <= 0 or not params_hash:
            raise ModelResearchError("每个因子必须锁定factor_id、factor_version和params_hash")
        if not isinstance(params, Mapping):
            raise ModelResearchError(f"因子{factor_id}必须冻结params")
        key = (factor_id, version, params_hash)
        if key in seen:
            continue
        seen.add(key)
        normalized_factors.append({
            "factor_id": factor_id,
            "factor_version": version,
            "params_hash": params_hash,
            "params": dict(params),
            "label": str(item.get("label") or factor_id),
            "category": str(item.get("category") or "custom"),
        })
    date_start = _iso_date(source.get("date_start"), "date_start")
    date_end = _iso_date(source.get("date_end"), "date_end")
    if date_start >= date_end:
        raise ModelResearchError("训练开始日期必须早于结束日期")
    data_cutoff = _iso_datetime(source.get("data_cutoff"), "data_cutoff")
    research_target = str(
        source.get("research_target") or "stock_selection"
    ).strip().lower()
    if research_target not in {
        "stock_selection", "market_style", "industry_rotation",
    }:
        raise ModelResearchError(
            "训练目标只支持stock_selection、market_style或industry_rotation"
        )
    if (
        research_target == "industry_rotation"
        and date_start < SW2021_INDUSTRY_SAFE_START
    ):
        raise ModelResearchError(
            "申万一级行业轮动仅支持2021-12-13及以后；"
            "更早历史包含申万2021版回溯重分类"
        )
    raw_horizon = source.get("label_horizon_trading_days")
    if raw_horizon is None:
        raw_horizon = dict(source.get("label") or {}).get(
            "horizon_trading_days", 5,
        )
    try:
        horizon = int(raw_horizon)
    except (TypeError, ValueError) as exc:
        raise ModelResearchError("单周期标签必须是T+1至T+30交易日") from exc
    if horizon < 1 or horizon > 30:
        raise ModelResearchError("单周期标签必须是T+1至T+30交易日")
    target_mode = str(
        source.get("target_mode")
        or dict(source.get("label") or {}).get("mode")
        or "return"
    ).strip().lower()
    if target_mode not in {"return", "classification"}:
        raise ModelResearchError("目标类型只支持return或classification")
    if target_mode == "classification":
        label = {
            "kind": f"future_{horizon}d_direction",
            "mode": "classification",
            "horizon_trading_days": horizon,
            "range": [0.0, 1.0],
            "classes": [0, 1],
            "positive_class": "future_return_gt_zero",
            "formula": f"1[future_return(T,T+{horizon}) > 0]",
        }
        if research_target == "market_style":
            label["entities"] = ["STYLE_SMALL", "STYLE_LARGE"]
        elif research_target == "industry_rotation":
            label.update({
                "classification": "sw2021_l1",
                "safe_start": SW2021_INDUSTRY_SAFE_START,
            })
    else:
        label = (
            {
                "kind": f"future_{horizon}d_market_style_rank",
                "horizon_trading_days": horizon,
                "range": [-1.0, 1.0],
                "entities": ["STYLE_SMALL", "STYLE_LARGE"],
            }
            if research_target == "market_style"
            else {
                "kind": f"future_{horizon}d_industry_rank",
                "horizon_trading_days": horizon,
                "range": [-1.0, 1.0],
                "classification": "sw2021_l1",
                "safe_start": SW2021_INDUSTRY_SAFE_START,
            }
            if research_target == "industry_rotation"
            else {
                "kind": f"future_{horizon}d_cross_sectional_rank",
                "horizon_trading_days": horizon,
                "range": [-1.0, 1.0],
            }
        )
    availability = {
        "event_available_at_lte_signal_close": True,
        "source_available_at_lte_data_cutoff": True,
    }
    if research_target == "industry_rotation":
        availability.update({
            "industry_snapshot_date_eq_signal_date": True,
            "industry_classification_safe_start": SW2021_INDUSTRY_SAFE_START,
        })
    raw_split = source.get("split") or {}
    try:
        valid_raw = raw_split.get("valid")
        test_raw = raw_split.get("test")
        valid_ratio = 0.2 if valid_raw is None else float(valid_raw)
        test_ratio = 0.2 if test_raw is None else float(test_raw)
    except (TypeError, ValueError) as exc:
        raise ModelResearchError("验证集/测试集比例必须是数字") from exc
    train_ratio = round(1.0 - valid_ratio - test_ratio, 6)
    ratios = (train_ratio, valid_ratio, test_ratio)
    if not all(math.isfinite(ratio) for ratio in ratios):
        raise ModelResearchError("验证集/测试集比例必须是有效数字")
    if valid_ratio < 0.05 or test_ratio < 0.05 or train_ratio < 0.3:
        raise ModelResearchError(
            "切分比例无效：验证集/测试集不低于5%，训练集不低于30%"
        )
    if abs(train_ratio + valid_ratio + test_ratio - 1.0) > 1e-6:
        raise ModelResearchError("训练/验证/测试比例之和必须为100%")
    universe_id = str(source.get("universe_id") or "csi500").strip()
    if universe_id not in UNIVERSES:
        raise ModelResearchError(
            f"不支持的股票池: {universe_id}，可选{', '.join(sorted(UNIVERSES))}"
        )
    index_code = UNIVERSES[universe_id]["index_code"]
    benchmark_code = UNIVERSES[universe_id]["benchmark"]
    raw_sample_filters = source.get("sample_filters")
    legacy_without_sample_filters = (
        raw_sample_filters is None
        and str(source.get("pipeline_version") or "")
        in {
            "alphablocks.dataset-pipeline.v1",
            "alphablocks.dataset-pipeline.v2",
        }
    )
    if raw_sample_filters is None:
        raw_sample_filters = {}
    if not isinstance(raw_sample_filters, Mapping):
        raise ModelResearchError("sample_filters必须是对象")
    try:
        minimum_listing_trading_days = int(
            raw_sample_filters.get(
                "minimum_listing_trading_days",
                0 if legacy_without_sample_filters else 60,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ModelResearchError("最少上市交易日必须是0至5000的整数") from exc
    if not 0 <= minimum_listing_trading_days <= 5000:
        raise ModelResearchError("最少上市交易日必须是0至5000的整数")

    def sample_filter_switch(name: str, default: bool) -> bool:
        value = raw_sample_filters.get(name, default)
        if not isinstance(value, bool):
            raise ModelResearchError(f"sample_filters.{name}必须是布尔值")
        return value

    try:
        custom_formulas = normalize_custom_sample_filters(
            raw_sample_filters.get("custom_formulas", []),
        )
    except ValueError as exc:
        raise ModelResearchError(str(exc)) from exc

    sample_filters = {
        "minimum_listing_trading_days": minimum_listing_trading_days,
        "exclude_st": sample_filter_switch(
            "exclude_st", not legacy_without_sample_filters,
        ),
        "exclude_delisting": sample_filter_switch(
            "exclude_delisting", not legacy_without_sample_filters,
        ),
        "custom_formulas": custom_formulas,
    }
    return {
        # Part of the immutable dataset identity. Bump this whenever label or
        # feature materialization semantics change so an older canonical
        # snapshot can never be silently reused by a newly created job.
        "pipeline_version": "alphablocks.dataset-pipeline.v5",
        "name": str(source.get("name") or f"{universe_id}因子数据集")[:160],
        "universe_id": universe_id,
        "index_code": index_code,
        "benchmark_code": benchmark_code,
        "sample_filters": sample_filters,
        "date_start": date_start,
        "date_end": date_end,
        "data_cutoff": data_cutoff,
        "research_target": research_target,
        "target_mode": target_mode,
        "prediction_scope": (
            "market_style" if research_target == "market_style"
            else "industry" if research_target == "industry_rotation"
            else "stock"
        ),
        "factors": normalized_factors,
        "feature_field": "score",
        "label": label,
        "split": {
            "train": train_ratio,
            "valid": valid_ratio,
            "test": test_ratio,
            "embargo_days": horizon,
        },
        "minimum_factor_coverage": 0.8,
        "materialization": {
            "mode": "on_demand",
            "format": "parquet",
            "persist_factor_values": False,
        },
        "availability": availability,
    }


def _model_spec(
    source: Mapping[str, Any], *, target_mode: str | None = None,
) -> dict[str, Any]:
    kind = str(source.get("kind") or "lightgbm").strip().lower()
    if kind == "stacking":
        base_sources = source.get("base_models")
        if (
            not isinstance(base_sources, list)
            or not 2 <= len(base_sources) <= 8
            or any(not isinstance(item, Mapping) for item in base_sources)
        ):
            raise ModelResearchError("Stacking必须配置2到8个基模型")
        base_models = [
            _model_spec(item, target_mode=target_mode) for item in base_sources
        ]
        base_kinds = [str(item["kind"]) for item in base_models]
        family = _stacking_family(base_kinds)
        raw_params = dict(source.get("params") or {})
        unknown = sorted(set(raw_params) - {
            "n_folds", "meta_alpha", "loss", "objective", "metric",
        })
        if unknown:
            raise ModelResearchError(
                "stacking参数包含未允许字段: " + ", ".join(unknown)
            )
        try:
            n_folds = int(raw_params.get("n_folds") or 3)
            meta_alpha = float(raw_params.get("meta_alpha") or 1.0)
        except (TypeError, ValueError) as exc:
            raise ModelResearchError("Stacking参数格式无效") from exc
        if not 2 <= n_folds <= 10:
            raise ModelResearchError("Stacking OOF折数必须在2到10之间")
        if not 0.01 <= meta_alpha <= 100.0:
            raise ModelResearchError("Stacking元学习器alpha必须在0.01到100之间")
        requested_loss = (
            "binary" if target_mode == "classification"
            else str(raw_params.get("loss") or "mse").strip().lower()
        )
        if requested_loss not in {"mse", "binary"}:
            raise ModelResearchError("stacking.params.loss只支持mse或binary")
        objective = "binary" if requested_loss == "binary" else "regression"
        metric = str(raw_params.get("metric") or (
            "auc" if requested_loss == "binary" else "rmse"
        )).strip().lower()
        supported_metrics = (
            {"auc", "binary_logloss"}
            if requested_loss == "binary"
            else {"l2", "rmse", "mae"}
        )
        if metric not in supported_metrics:
            raise ModelResearchError("Metric必须与Objective一致")
        return {
            "kind": "stacking",
            "qlib_model": "factor_service.research.trainer.QlibStackingModel",
            "params": {
                "n_folds": n_folds,
                "meta_alpha": meta_alpha,
                "loss": requested_loss,
                "objective": objective,
                "metric": metric,
            },
            "base_models": base_models,
            "ensemble": {
                "method": "stacking",
                "family": family,
                "base_model_kinds": base_kinds,
            },
        }
    definitions: dict[str, dict[str, Any]] = {
        "lightgbm": {
            "qlib_model": "qlib.contrib.model.gbdt.LGBModel",
            "allowed": {
                "learning_rate", "num_leaves", "max_depth", "n_estimators",
                "subsample", "colsample_bytree", "reg_alpha", "reg_lambda",
                "min_child_samples", "min_data_in_leaf", "path_smooth",
                "bagging_freq", "lambda_l1", "lambda_l2", "feature_fraction",
                "bagging_fraction", "early_stopping_rounds", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.02, "num_leaves": 31,
                "max_depth": -1, "n_estimators": 2000,
                "min_data_in_leaf": 300, "min_child_samples": 150,
                "path_smooth": 1.0, "bagging_freq": 5,
                "lambda_l1": 0.5, "lambda_l2": 1.0,
                "feature_fraction": 0.7, "bagging_fraction": 0.8,
                "early_stopping_rounds": 50,
            },
        },
        "xgboost": {
            "qlib_model": "qlib.contrib.model.xgboost.XGBModel",
            "allowed": {
                "learning_rate", "max_depth", "n_estimators", "subsample",
                "colsample_bytree", "reg_alpha", "reg_lambda",
                "min_child_weight", "early_stopping_rounds", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.02, "max_depth": 4,
                "n_estimators": 2000, "subsample": 0.7,
                "colsample_bytree": 0.65, "reg_alpha": 0.5, "reg_lambda": 2.0,
                "min_child_weight": 100.0, "early_stopping_rounds": 50,
            },
        },
        "catboost": {
            "qlib_model": "qlib.contrib.model.catboost_model.CatBoostModel",
            "allowed": {
                "learning_rate", "depth", "n_estimators", "l2_leaf_reg",
                "random_strength", "bagging_temperature", "od_wait",
                "early_stopping_rounds", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.02, "depth": 6,
                "n_estimators": 2000, "l2_leaf_reg": 3.0,
                "random_strength": 1.5, "bagging_temperature": 0.8,
                "od_wait": 100, "early_stopping_rounds": 50,
            },
        },
        "random_forest": {
            "qlib_model": "factor_service.research.models.QlibSklearnRandomForestModel",
            "allowed": {
                "n_estimators", "max_depth", "min_samples_split",
                "min_samples_leaf", "max_features", "num_threads",
            },
            "defaults": {
                "loss": "mse", "n_estimators": 300, "max_depth": 0,
                "min_samples_split": 2, "min_samples_leaf": 1,
                "max_features": "sqrt",
            },
        },
        "linear": {
            "qlib_model": "factor_service.research.models.QlibSklearnRidgeModel",
            "allowed": {
                "alpha", "fit_intercept", "solver", "max_iter", "num_threads",
            },
            "defaults": {
                "loss": "mse", "alpha": 3.0, "fit_intercept": True,
                "solver": "auto", "max_iter": 1000,
            },
        },
        "mlp": {
            "qlib_model": "factor_service.research.models.QlibTorchMLPModel",
            "allowed": {
                "learning_rate", "hidden_layers", "hidden_size", "layer_count", "max_steps",
                "batch_size", "early_stopping_rounds", "eval_steps",
                "weight_decay", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.0001,
                "hidden_size": 64, "layer_count": 2,
                "max_steps": 200, "batch_size": 4000,
                "early_stopping_rounds": 20, "eval_steps": 10,
                "weight_decay": 0.0001,
            },
        },
        "gru": {
            "qlib_model": "factor_service.research.models.QlibTorchGRUModel",
            "allowed": {
                "learning_rate", "lookback_window", "hidden_size", "num_layers",
                "dropout", "max_steps", "batch_size", "early_stopping_rounds",
                "eval_steps", "weight_decay", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.001,
                "lookback_window": 20, "hidden_size": 64,
                "num_layers": 2, "dropout": 0.2,
                "max_steps": 200, "batch_size": 4000,
                "early_stopping_rounds": 20, "eval_steps": 10,
                "weight_decay": 0.0001,
            },
        },
        "lstm": {
            "qlib_model": "factor_service.research.models.QlibTorchLSTMModel",
            "allowed": {
                "learning_rate", "lookback_window", "hidden_size", "num_layers",
                "dropout", "max_steps", "batch_size", "early_stopping_rounds",
                "eval_steps", "weight_decay", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.001,
                "lookback_window": 20, "hidden_size": 64,
                "num_layers": 2, "dropout": 0.2,
                "max_steps": 200, "batch_size": 4000,
                "early_stopping_rounds": 20, "eval_steps": 10,
                "weight_decay": 0.0001,
            },
        },
        "alstm": {
            "qlib_model": "factor_service.research.models.QlibTorchALSTMModel",
            "allowed": {
                "learning_rate", "lookback_window", "hidden_size", "num_layers",
                "dropout", "max_steps", "batch_size", "early_stopping_rounds",
                "eval_steps", "weight_decay", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.001,
                "lookback_window": 20, "hidden_size": 64,
                "num_layers": 2, "dropout": 0.2,
                "max_steps": 200, "batch_size": 4000,
                "early_stopping_rounds": 20, "eval_steps": 10,
                "weight_decay": 0.0001,
            },
        },
        "transformer": {
            "qlib_model": "factor_service.research.models.QlibTorchTransformerModel",
            "allowed": {
                "learning_rate", "lookback_window", "d_model", "nhead",
                "transformer_layers", "dim_feedforward", "dropout",
                "max_steps", "batch_size", "early_stopping_rounds",
                "eval_steps", "weight_decay", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.0001,
                "lookback_window": 20, "d_model": 64, "nhead": 4,
                "transformer_layers": 2, "dim_feedforward": 256,
                "dropout": 0.2, "max_steps": 200, "batch_size": 4000,
                "early_stopping_rounds": 20, "eval_steps": 10,
                "weight_decay": 0.0001,
            },
        },
        "tabnet": {
            "qlib_model": "factor_service.research.models.QlibNativeTabNetAdapter",
            "allowed": {
                "learning_rate", "n_d", "n_a", "n_steps", "n_shared",
                "n_ind", "batch_size", "max_steps", "early_stopping_rounds",
                "pretrain", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.005, "n_d": 64, "n_a": 64,
                "n_steps": 5, "n_shared": 2, "n_ind": 2,
                "batch_size": 4000, "max_steps": 200,
                "early_stopping_rounds": 20, "pretrain": False,
            },
        },
        "tcn": {
            "qlib_model": "factor_service.research.models.QlibTorchTCNModel",
            "allowed": {
                "learning_rate", "lookback_window", "hidden_size",
                "kernel_size", "num_layers", "dropout", "max_steps",
                "batch_size", "early_stopping_rounds", "eval_steps",
                "weight_decay", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.0001,
                "lookback_window": 20, "hidden_size": 128,
                "kernel_size": 5, "num_layers": 2, "dropout": 0.2,
                "max_steps": 200, "batch_size": 4000,
                "early_stopping_rounds": 20, "eval_steps": 10,
                "weight_decay": 0.0001,
            },
        },
        "nativetft": {
            "qlib_model": "factor_service.research.models.QlibTorchNativeTFTModel",
            "allowed": {
                "learning_rate", "lookback_window", "d_model", "nhead",
                "gru_hidden_size", "num_layers", "dim_feedforward", "dropout",
                "max_steps", "batch_size", "early_stopping_rounds",
                "eval_steps", "weight_decay", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.0005,
                "lookback_window": 20, "d_model": 64, "nhead": 4,
                "gru_hidden_size": 64, "num_layers": 2,
                "dim_feedforward": 128, "dropout": 0.2,
                "max_steps": 200, "batch_size": 4000,
                "early_stopping_rounds": 20, "eval_steps": 10,
                "weight_decay": 0.0001,
            },
        },
        "transformer_lstm": {
            "qlib_model": "factor_service.research.models.QlibTorchTransformerLSTMModel",
            "allowed": {
                "learning_rate", "lookback_window", "d_model", "nhead",
                "transformer_layers", "dim_feedforward", "lstm_hidden_size",
                "lstm_layers", "dropout", "max_steps", "batch_size",
                "early_stopping_rounds", "eval_steps", "weight_decay", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.001,
                "lookback_window": 60, "d_model": 64, "nhead": 4,
                "transformer_layers": 2, "dim_feedforward": 256,
                "lstm_hidden_size": 128, "lstm_layers": 1, "dropout": 0.2,
                "max_steps": 300, "batch_size": 256,
                "early_stopping_rounds": 10, "eval_steps": 10,
                "weight_decay": 0.0001,
            },
        },
    }
    if kind not in definitions:
        raise ModelResearchError(
            "model.kind只允许lightgbm、xgboost、catboost、random_forest、"
            "linear、mlp、gru、lstm、alstm、transformer、tabnet、tcn、"
            "nativetft或transformer_lstm"
        )
    definition = definitions[kind]
    allowed = {*definition["allowed"], "loss", "objective", "metric"}
    params = {key: value for key, value in dict(source.get("params") or {}).items() if key in allowed}
    if kind == "mlp" and "hidden_layers" in params:
        layers = params["hidden_layers"]
        if not isinstance(layers, list) or not 1 <= len(layers) <= 8:
            raise ModelResearchError("hidden_layers必须是包含1到8层的数组")
        try:
            normalized_layers = [int(width) for width in layers]
        except (TypeError, ValueError) as exc:
            raise ModelResearchError("hidden_layers每层宽度必须是整数") from exc
        if any(width < 4 or width > 4096 for width in normalized_layers):
            raise ModelResearchError("hidden_layers每层宽度必须在4到4096之间")
        params["hidden_layers"] = normalized_layers
    defaults = {
        **definition["defaults"],
        "num_threads": max(1, min(int(params.get("num_threads") or 4), 32)),
        "seed": 42,
        "deterministic": True,
        "verbosity": -1,
    }
    if kind == "lightgbm":
        defaults.update({
            "feature_fraction_seed": 42,
            "bagging_seed": 42,
            "data_random_seed": 42,
        })
    defaults.update(params)
    if kind == "mlp" and "hidden_layers" in params:
        defaults.pop("hidden_size", None)
        defaults.pop("layer_count", None)
    requested_loss = str(defaults.get("loss") or "mse").strip().lower()
    if target_mode is not None:
        requested_loss = "binary" if target_mode == "classification" else "mse"
    if requested_loss not in {"mse", "binary"}:
        raise ModelResearchError("model.params.loss只支持mse或binary")
    defaults["loss"] = requested_loss
    expected_objective = "binary" if requested_loss == "binary" else "regression"
    requested_objective = str(
        defaults.get("objective") or expected_objective
    ).strip().lower()
    if target_mode is not None and "objective" in params and requested_objective != expected_objective:
        raise ModelResearchError("Objective必须与目标类型一致")
    if requested_objective not in {"regression", "binary"}:
        raise ModelResearchError("model.params.objective只支持regression或binary")
    defaults["objective"] = expected_objective
    requested_metric = str(
        defaults.get("metric") or ("auc" if requested_loss == "binary" else "rmse")
    ).strip().lower()
    supported_metrics = (
        {"auc", "binary_logloss"}
        if requested_loss == "binary"
        else {"l2", "rmse", "mae"}
    )
    if requested_metric not in supported_metrics:
        raise ModelResearchError("Metric必须与Objective一致")
    defaults["metric"] = requested_metric
    if kind in {"transformer", "nativetft", "transformer_lstm"}:
        try:
            d_model = int(defaults["d_model"])
            nhead = int(defaults["nhead"])
        except (TypeError, ValueError) as exc:
            raise ModelResearchError("d_model和nhead必须是整数") from exc
        if nhead < 1 or d_model % nhead != 0:
            raise ModelResearchError("d_model必须能被nhead整除")
        defaults["d_model"] = d_model
        defaults["nhead"] = nhead
    return {"kind": kind, "qlib_model": definition["qlib_model"], "params": defaults}


def _execution_spec(source: Mapping[str, Any]) -> dict[str, Any]:
    node_id = str(source.get("node_id") or "local").strip()
    if node_id != "local" and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", node_id,
    ):
        raise ModelResearchError("execution.node_id无效")
    try:
        max_runtime_minutes = int(source.get("max_runtime_minutes") or 720)
    except (TypeError, ValueError) as exc:
        raise ModelResearchError("execution.max_runtime_minutes必须是整数") from exc
    if not 60 <= max_runtime_minutes <= 1440:
        raise ModelResearchError("execution.max_runtime_minutes必须在60到1440之间")
    return {
        "node_id": node_id,
        "mode": "local" if node_id == "local" else "remote_ssh_docker",
        "max_runtime_minutes": max_runtime_minutes,
    }


def _stacking_family(model_kinds: list[str]) -> str:
    kinds = {str(kind).strip().lower() for kind in model_kinds}
    if kinds and kinds <= CLASSICAL_STACKING_KINDS:
        return "classical"
    if kinds and kinds <= DEEP_STACKING_KINDS:
        return "deep_learning"
    raise ModelResearchError(
        "Stacking只支持同一模型族多选；传统模型与深度学习模型不能混合集成"
    )


def _walk_forward_spec(source: Mapping[str, Any]) -> dict[str, Any]:
    enabled = bool(source.get("enabled", False))
    strategy = str(source.get("strategy") or "rolling").strip().lower()
    if strategy not in {"rolling", "expanding"}:
        raise ModelResearchError("Walk-Forward策略只允许rolling或expanding")

    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(source.get(name, default))
        except (TypeError, ValueError) as exc:
            raise ModelResearchError(f"Walk-Forward {name}必须是整数") from exc
        if not minimum <= value <= maximum:
            raise ModelResearchError(
                f"Walk-Forward {name}必须在{minimum}到{maximum}之间"
            )
        return value

    train_years = integer("train_years", 3, 1, 8)
    valid_months = integer("valid_months", 6, 1, 36)
    test_months = integer("test_months", 12, 1, 36)
    step_months = integer("step_months", 12, 1, 36)
    max_windows = integer("max_windows", 4, 1, 12)
    embargo_days = integer("embargo_days", 5, 1, 30)
    if step_months < test_months:
        raise ModelResearchError("Walk-Forward步长不得小于测试窗口，避免样本外预测重叠")
    return {
        "enabled": enabled,
        "strategy": strategy,
        "train_years": train_years,
        "valid_months": valid_months,
        "test_months": test_months,
        "step_months": step_months,
        "max_windows": max_windows,
        "embargo_days": embargo_days,
        "session_convention": {"year": 252, "month": 21},
    }


def _model_payload_references(
    model: Mapping[str, Any], *, model_id: str, model_version: int,
) -> bool:
    """Return whether a frozen model depends on another immutable version."""

    manifest = dict(model.get("manifest_json") or {})
    config = dict(model.get("config_json") or {})
    reference_objects: list[Mapping[str, Any]] = []
    for payload in (manifest, config):
        ensemble = dict(payload.get("ensemble") or {})
        reference_objects.extend(
            item for item in ensemble.get("sources") or []
            if isinstance(item, Mapping)
        )
    for key in ("incremental_training", "research_origin"):
        item = config.get(key) or {}
        if isinstance(item, Mapping):
            reference_objects.append(item)
    for item in reference_objects:
        referenced_id = str(
            item.get("model_id") or item.get("source_model_id") or ""
        )
        try:
            referenced_version = int(
                item.get("model_version")
                or item.get("source_model_version")
                or item.get("version")
                or 0
            )
        except (TypeError, ValueError):
            continue
        if referenced_id == str(model_id) and referenced_version == int(model_version):
            return True
    return False


def _job_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result.pop("lease_token", None)
    # Repository rows are also the durable worker transport payload. psycopg
    # returns timestamp columns as datetime objects, which cannot be written to
    # the worker state JSON or isolated-process descriptor without conversion.
    for key, value in tuple(result.items()):
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    if (
        str(result.get("kind") or "train") == "train"
        and str(result.get("status") or "") == "succeeded"
    ):
        config = dict(result.get("config_json") or {})
        result["planned_model_version"] = int(
            config.get("planned_model_version") or 0
        )
        result["registration_status"] = (
            "registered"
            if int(result.get("model_version") or 0) > 0
            else "declined"
            if str(
                ((result.get("result_json") or {}).get("registration") or {}).get("status")
                or ""
            ) == "declined"
            else "pending_confirmation"
        )
    return result


def _required_identifier(value: str, field: str) -> str:
    clean = _clean_identifier(value)
    if not clean:
        raise ModelResearchError(f"{field}不能为空")
    return clean


def _clean_identifier(value: str, *, default: str = "") -> str:
    clean = "".join(character for character in str(value).strip() if character.isalnum() or character in "._-")
    return (clean or default)[:128]


def _iso_date(value: Any, field: str) -> str:
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except (TypeError, ValueError) as exc:
        raise ModelResearchError(f"{field}不是有效日期") from exc


def _iso_datetime(value: Any, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ModelResearchError(f"{field}不是有效时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "ModelResearchConflict",
    "ModelResearchError",
    "ModelResearchNotFound",
    "ModelResearchRepository",
]
