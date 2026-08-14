from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import secrets
from typing import Any, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from factor_service.control_database import ControlDatabase, get_control_database


ACTIVE_STATUSES = frozenset({"leased", "running", "uploading"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})


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
        model = _model_spec(payload.get("model") or {})
        spec_json = _canonical_json(spec)
        spec_hash = sha256(spec_json.encode("utf-8")).hexdigest()
        dataset_id = f"dataset_{spec_hash[:24]}"
        job_id = f"model_job_{uuid4().hex}"
        model_id = _clean_identifier(
            str(payload.get("model_id") or ""),
            default=f"model_{uuid4().hex[:16]}",
        )
        title = str(payload.get("title") or f"{model['kind']} 因子模型").strip()[:160]
        config = {
            "schema_version": "alphablocks.model-training.v1",
            "dataset": spec,
            "model": model,
            "backtest": {
                "universe_id": "csi500",
                "top_n": 20,
                "rebalance_every": 5,
                "benchmark_code": "000905.SH",
            },
        }
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
                    payload={"dataset_hash": spec_hash},
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

    def list_jobs(self, *, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        values: list[Any] = []
        where = ""
        if status:
            where = "WHERE jobs.status = %s"
            values.append(status)
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
    ) -> dict[str, Any]:
        now = _utcnow()
        expires = now + timedelta(seconds=max(30, min(int(lease_seconds), 300)))
        with self.database.connection() as conn:
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
                (expires, Jsonb(dict(progress or {})), now, job_id, "alpha-factor-service", lease_token),
            ).fetchone()
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
                metrics = dict(result.get("metrics") or {})
                importance = list(result.get("feature_importance") or [])
                predictions = dict(result.get("predictions") or {})
                manifest = dict(result.get("manifest") or {})
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
                        row["model_kind"], Jsonb(metrics), Jsonb(importance),
                        Jsonb(predictions), Jsonb(manifest), now, now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE model_jobs
                    SET status = 'succeeded', result_json = %s, model_version = %s,
                        lease_expires_at = NULL, finished_at = %s, updated_at = %s
                    WHERE job_id = %s
                    """,
                    (Jsonb(dict(result)), version, now, now, job_id),
                )
                conn.execute(
                    "UPDATE model_artifacts SET model_version = %s WHERE job_id = %s",
                    (version, job_id),
                )
                self._event(
                    conn, job_id, "job.succeeded", stage="succeeded",
                    payload={"model_id": model_id, "model_version": version},
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
                SELECT versions.*, specs.spec_hash AS dataset_hash
                FROM model_versions versions
                JOIN model_dataset_specs specs USING(dataset_id)
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
                       specs.spec_json AS dataset_spec
                FROM model_versions versions
                JOIN model_dataset_specs specs USING(dataset_id)
                WHERE model_id = %s AND version = %s
                """,
                (model_id, int(version)),
            ).fetchone()
        if not row:
            raise ModelResearchNotFound("模型版本不存在")
        return dict(row)

    def mark_validated(self, model_id: str, version: int, factor_backtest_job_id: str) -> dict[str, Any]:
        now = _utcnow()
        with self.database.connection() as conn:
            row = conn.execute(
                """
                UPDATE model_versions SET state = 'validated', updated_at = %s
                WHERE model_id = %s AND version = %s
                RETURNING *
                """,
                (now, model_id, int(version)),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE model_jobs SET factor_backtest_job_id = %s, updated_at = %s
                    WHERE job_id = %s
                    """,
                    (factor_backtest_job_id, now, row["job_id"]),
                )
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
        if not row:
            raise ModelResearchNotFound("模型版本不存在")
        return dict(row)

    def list_inference_schedules(self) -> list[dict[str, Any]]:
        with self.database.connection() as conn:
            rows = conn.execute(
                """
                SELECT schedules.*, versions.name, versions.model_kind, versions.state,
                       versions.prediction_json, specs.spec_json AS dataset_spec
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


def _dataset_spec(source: Mapping[str, Any]) -> dict[str, Any]:
    factors = source.get("factors")
    if not isinstance(factors, list) or not factors:
        raise ModelResearchError("至少选择一个因子")
    if len(factors) > 100:
        raise ModelResearchError("一次最多选择100个因子")
    normalized_factors = []
    seen: set[tuple[str, int, str]] = set()
    for item in factors:
        item = item if isinstance(item, Mapping) else {}
        factor_id = str(item.get("factor_id") or "").strip()
        version = int(item.get("factor_version") or item.get("version") or 0)
        params_hash = str(item.get("params_hash") or "").strip()
        if not factor_id or version <= 0 or not params_hash:
            raise ModelResearchError("每个因子必须锁定factor_id、factor_version和params_hash")
        key = (factor_id, version, params_hash)
        if key in seen:
            continue
        seen.add(key)
        normalized_factors.append({
            "factor_id": factor_id,
            "factor_version": version,
            "params_hash": params_hash,
            "label": str(item.get("label") or factor_id),
            "category": str(item.get("category") or "custom"),
        })
    date_start = _iso_date(source.get("date_start"), "date_start")
    date_end = _iso_date(source.get("date_end"), "date_end")
    if date_start >= date_end:
        raise ModelResearchError("训练开始日期必须早于结束日期")
    data_cutoff = _iso_datetime(source.get("data_cutoff"), "data_cutoff")
    return {
        "name": str(source.get("name") or "中证500因子数据集")[:160],
        "universe_id": "csi500",
        "index_code": "000905.SH",
        "date_start": date_start,
        "date_end": date_end,
        "data_cutoff": data_cutoff,
        "factors": normalized_factors,
        "feature_field": "score",
        "label": {
            "kind": "future_5d_cross_sectional_rank",
            "horizon_trading_days": 5,
            "range": [-1.0, 1.0],
        },
        "split": {"train": 0.6, "valid": 0.2, "test": 0.2, "embargo_days": 5},
        "minimum_factor_coverage": 0.8,
        "availability": {
            "event_available_at_lte_signal_close": True,
            "computed_at_lte_data_cutoff": True,
        },
    }


def _model_spec(source: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(source.get("kind") or "lightgbm").strip().lower()
    definitions: dict[str, dict[str, Any]] = {
        "lightgbm": {
            "qlib_model": "qlib.contrib.model.gbdt.LGBModel",
            "allowed": {
                "learning_rate", "num_leaves", "max_depth", "n_estimators",
                "subsample", "colsample_bytree", "reg_alpha", "reg_lambda",
                "min_child_samples", "early_stopping_rounds", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.05, "num_leaves": 31,
                "max_depth": -1, "n_estimators": 1000, "subsample": 0.9,
                "colsample_bytree": 0.9, "reg_alpha": 0.0, "reg_lambda": 0.0,
                "min_child_samples": 20, "early_stopping_rounds": 50,
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
                "loss": "mse", "learning_rate": 0.05, "max_depth": 6,
                "n_estimators": 1000, "subsample": 0.9,
                "colsample_bytree": 0.9, "reg_alpha": 0.0, "reg_lambda": 1.0,
                "min_child_weight": 1.0, "early_stopping_rounds": 50,
            },
        },
        "catboost": {
            "qlib_model": "qlib.contrib.model.catboost_model.CatBoostModel",
            "allowed": {
                "learning_rate", "depth", "n_estimators", "l2_leaf_reg",
                "random_strength", "early_stopping_rounds", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.05, "depth": 6,
                "n_estimators": 1000, "l2_leaf_reg": 3.0,
                "random_strength": 1.0, "early_stopping_rounds": 50,
            },
        },
        "mlp": {
            "qlib_model": "factor_service.research.models.QlibTorchMLPModel",
            "allowed": {
                "learning_rate", "hidden_size", "layer_count", "max_steps",
                "batch_size", "early_stopping_rounds", "eval_steps",
                "weight_decay", "num_threads",
            },
            "defaults": {
                "loss": "mse", "learning_rate": 0.001, "hidden_size": 64,
                "layer_count": 2, "max_steps": 300, "batch_size": 2048,
                "early_stopping_rounds": 10, "eval_steps": 10,
                "weight_decay": 0.0001,
            },
        },
    }
    if kind not in definitions:
        raise ModelResearchError("model.kind只允许lightgbm、xgboost、catboost或mlp")
    definition = definitions[kind]
    allowed = definition["allowed"]
    params = {key: value for key, value in dict(source.get("params") or {}).items() if key in allowed}
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
    return {"kind": kind, "qlib_model": definition["qlib_model"], "params": defaults}


def _job_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result.pop("lease_token", None)
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
