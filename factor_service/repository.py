from __future__ import annotations

import json
import hashlib
import math
from datetime import date, datetime
from typing import Any, Optional
from uuid import uuid4

from factor_service.clickhouse import client, settings
from factor_service.qlib_formula import compile_qlib_formula
from factor_service.schemas import (
    FactorAnalysisIcOut,
    FactorAnalysisJobCreate,
    FactorAnalysisJobOut,
    FactorAnalysisQuantileReturnOut,
    FactorAnalysisSummaryOut,
    FactorAnalysisTurnoverOut,
    CoverageOut,
    FactorCreate,
    FactorJobCreate,
    FactorJobOut,
    FactorOut,
    FactorUpdate,
    FactorValueMetricQualityOut,
    FactorValueQualityOut,
    FactorValueOut,
)


def list_factors(
    entity_type: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> list[FactorOut]:
    database = settings().clickhouse_database
    conditions = []
    params = {}
    if entity_type:
        conditions.append("entity_type = {entity_type:String}")
        params["entity_type"] = entity_type
    if enabled is not None:
        conditions.append("enabled = {enabled:UInt8}")
        params["enabled"] = 1 if enabled else 0
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.factor_definitions
        {where}
        ORDER BY factor_id ASC, version DESC
        LIMIT 1 BY factor_id
        """,
        parameters=params,
    ).result_rows
    versions = _factor_versions(
        [str(row[0]) for row in rows],
    )
    return [
        _factor_from_row(row, available_versions=versions.get(str(row[0]), ()))
        for row in rows
    ]


def get_factor(
    factor_id: str,
    version: Optional[int] = None,
) -> Optional[FactorOut]:
    database = settings().clickhouse_database
    version_condition = (
        "AND version = {version:UInt32}"
        if version is not None
        else ""
    )
    parameters = {"factor_id": factor_id}
    if version is not None:
        parameters["version"] = int(version)
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.factor_definitions
        WHERE factor_id = {{factor_id:String}}
        {version_condition}
        ORDER BY version DESC
        LIMIT 1
        """,
        parameters=parameters,
    ).result_rows
    if not rows:
        return None
    versions = _factor_versions([factor_id]).get(factor_id, ())
    return _factor_from_row(rows[0], available_versions=versions)


def _factor_versions(
    factor_ids: list[str],
) -> dict[str, tuple[int, ...]]:
    cleaned = sorted({str(item) for item in factor_ids if str(item)})
    if not cleaned:
        return {}
    database = settings().clickhouse_database
    rows = client().query(
        f"""
        SELECT factor_id, groupUniqArray(version)
        FROM {database}.factor_definitions
        WHERE factor_id IN {{factor_ids:Array(String)}}
        GROUP BY factor_id
        """,
        parameters={"factor_ids": cleaned},
    ).result_rows
    return {
        str(row[0]): tuple(sorted((int(item) for item in row[1]), reverse=True))
        for row in rows
    }


def create_factor(payload: FactorCreate) -> FactorOut:
    existing = get_factor(payload.factor_id)
    if existing:
        raise ValueError(f"因子已存在: {payload.factor_id}")
    payload = _validated_factor_payload(payload)
    _insert_factor(payload, version=1, created_at=datetime.now(), updated_at=datetime.now())
    factor = get_factor(payload.factor_id)
    if not factor:
        raise RuntimeError("因子创建后读取失败")
    return factor


def update_factor(factor_id: str, payload: FactorUpdate) -> FactorOut:
    current = get_factor(factor_id)
    if not current:
        raise ValueError(f"因子不存在: {factor_id}")
    data = current.model_dump()
    data.update({key: value for key, value in payload.model_dump(exclude_unset=True).items() if value is not None})
    data["factor_id"] = factor_id
    next_payload = FactorCreate(**{key: value for key, value in data.items() if key in FactorCreate.model_fields})
    next_payload = _validated_factor_payload(next_payload)
    _insert_factor(
        next_payload,
        version=current.version + 1,
        created_at=current.created_at or datetime.now(),
        updated_at=datetime.now(),
    )
    factor = get_factor(factor_id)
    if not factor:
        raise RuntimeError("因子更新后读取失败")
    return factor


def disable_factor(factor_id: str) -> FactorOut:
    return update_factor(factor_id, FactorUpdate(enabled=False))


def _validated_factor_payload(payload: FactorCreate) -> FactorCreate:
    if not payload.expression.strip():
        raise ValueError("因子表达式不能为空")
    try:
        compiled = compile_qlib_formula(
            payload.expression,
            params=payload.params,
            code_column="code",
            date_column="trade_date",
        )
    except ValueError as exc:
        raise ValueError(f"因子表达式不合法: {exc}") from exc
    asset_id = str(payload.asset_id or "").strip()
    source_node_id = str(payload.source_node_id or "").strip()
    if not asset_id:
        raise ValueError("因子必须绑定 entity asset")
    if not source_node_id:
        raise ValueError("因子必须绑定 source node")
    if str(payload.entity_type or "").strip() != asset_id:
        raise ValueError("entity_type 必须与 asset_id 一致")
    param_schema = (
        _validated_param_schema(payload.param_schema, payload.params)
        if payload.param_schema
        else _inferred_param_schema(payload.params)
    )
    availability_policy = dict(payload.availability_policy or {})
    if (
        str(availability_policy.get("field") or "") != "available_at"
        or str(availability_policy.get("policy") or "")
        != "persisted_timestamp"
    ):
        raise ValueError(
            "availability_policy 必须使用持久化 available_at 时间戳"
        )
    return payload.model_copy(
        update={
            "asset_id": asset_id,
            "source_node_id": source_node_id,
            "required_fields": compiled.fields,
            "param_schema": param_schema,
            "availability_policy": availability_policy,
        }
    )


def create_job(payload: FactorJobCreate) -> FactorJobOut:
    factor = get_factor(
        payload.factor_id,
        version=payload.factor_version,
    )
    if not factor:
        raise ValueError(f"因子不存在: {payload.factor_id}")
    if str(payload.entity_type or "").strip() != factor.asset_id:
        raise ValueError("任务 entity_type 必须与因子 asset_id 一致")
    _normalize_factor_params(
        factor.param_schema,
        factor.params,
        payload.params,
    )
    version = factor.version
    job_id = f"factor_job_{uuid4().hex}"
    now = datetime.now()
    database = settings().clickhouse_database
    row = [
        job_id,
        payload.factor_id,
        version,
        payload.entity_type,
        payload.mode,
        payload.universe,
        payload.date_start,
        payload.date_end,
        json.dumps(payload.params, ensure_ascii=False, sort_keys=True),
        "pending",
        "",
        None,
        now,
        None,
        None,
        now,
    ]
    client().insert(
        f"{database}.factor_compute_jobs",
        [row],
        column_names=[
            "job_id",
            "factor_id",
            "factor_version",
            "entity_type",
            "mode",
            "universe",
            "date_start",
            "date_end",
            "params_json",
            "status",
            "error_message",
            "row_count",
            "created_at",
            "started_at",
            "finished_at",
            "updated_at",
        ],
    )
    job = get_job(job_id)
    if not job:
        raise RuntimeError("任务创建后读取失败")
    return job


def list_jobs(
    factor_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
) -> list[FactorJobOut]:
    database = settings().clickhouse_database
    conditions = []
    params = {"limit": max(1, min(limit, 1000))}
    if factor_id:
        conditions.append("factor_id = {factor_id:String}")
        params["factor_id"] = factor_id
    if status:
        conditions.append("status = {status:String}")
        params["status"] = status
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.factor_compute_jobs FINAL
        {where}
        ORDER BY created_at DESC
        LIMIT {{limit:UInt32}}
        """,
        parameters=params,
    ).result_rows
    return [_job_from_row(row) for row in rows]


def get_job(job_id: str) -> Optional[FactorJobOut]:
    database = settings().clickhouse_database
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.factor_compute_jobs FINAL
        WHERE job_id = {{job_id:String}}
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        parameters={"job_id": job_id},
    ).result_rows
    return _job_from_row(rows[0]) if rows else None


def update_job_status(
    job_id: str,
    status: str,
    *,
    error_message: str = "",
    row_count: Optional[int] = None,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
) -> FactorJobOut:
    current = get_job(job_id)
    if not current:
        raise ValueError(f"任务不存在: {job_id}")
    now = datetime.now()
    database = settings().clickhouse_database
    row = [
        current.job_id,
        current.factor_id,
        current.factor_version,
        current.entity_type,
        current.mode,
        current.universe,
        current.date_start,
        current.date_end,
        json.dumps(current.params, ensure_ascii=False, sort_keys=True),
        status,
        error_message,
        row_count,
        current.created_at or now,
        started_at if started_at is not None else current.started_at,
        finished_at if finished_at is not None else current.finished_at,
        now,
    ]
    client().insert(
        f"{database}.factor_compute_jobs",
        [row],
        column_names=[
            "job_id",
            "factor_id",
            "factor_version",
            "entity_type",
            "mode",
            "universe",
            "date_start",
            "date_end",
            "params_json",
            "status",
            "error_message",
            "row_count",
            "created_at",
            "started_at",
            "finished_at",
            "updated_at",
        ],
    )
    updated = get_job(job_id)
    if not updated:
        raise RuntimeError("任务状态更新后读取失败")
    return updated


def create_analysis_job(payload: FactorAnalysisJobCreate) -> FactorAnalysisJobOut:
    factor = get_factor(payload.factor_id)
    if not factor:
        raise ValueError(f"因子不存在: {payload.factor_id}")
    periods = _clean_periods(payload.periods)
    analysis_job_id = f"factor_analysis_{uuid4().hex}"
    now = datetime.now()
    database = settings().clickhouse_database
    row = [
        analysis_job_id,
        payload.factor_id,
        payload.factor_version or factor.version,
        payload.entity_type,
        payload.params_hash,
        payload.date_start,
        payload.date_end,
        periods,
        payload.quantiles,
        payload.price_field,
        1 if payload.cumulative_returns else 0,
        payload.max_loss,
        "pending",
        "",
        None,
        now,
        None,
        None,
        now,
    ]
    client().insert(
        f"{database}.factor_analysis_jobs",
        [row],
        column_names=[
            "analysis_job_id",
            "factor_id",
            "factor_version",
            "entity_type",
            "params_hash",
            "date_start",
            "date_end",
            "periods",
            "quantiles",
            "price_field",
            "cumulative_returns",
            "max_loss",
            "status",
            "error_message",
            "row_count",
            "created_at",
            "started_at",
            "finished_at",
            "updated_at",
        ],
    )
    job = get_analysis_job(analysis_job_id)
    if not job:
        raise RuntimeError("分析任务创建后读取失败")
    return job


def list_analysis_jobs(
    factor_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
) -> list[FactorAnalysisJobOut]:
    database = settings().clickhouse_database
    conditions = []
    params = {"limit": max(1, min(limit, 1000))}
    if factor_id:
        conditions.append("factor_id = {factor_id:String}")
        params["factor_id"] = factor_id
    if status:
        conditions.append("status = {status:String}")
        params["status"] = status
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.factor_analysis_jobs FINAL
        {where}
        ORDER BY created_at DESC
        LIMIT {{limit:UInt32}}
        """,
        parameters=params,
    ).result_rows
    return [_analysis_job_from_row(row) for row in rows]


def get_analysis_job(analysis_job_id: str) -> Optional[FactorAnalysisJobOut]:
    database = settings().clickhouse_database
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.factor_analysis_jobs FINAL
        WHERE analysis_job_id = {{analysis_job_id:String}}
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        parameters={"analysis_job_id": analysis_job_id},
    ).result_rows
    return _analysis_job_from_row(rows[0]) if rows else None


def update_analysis_job_status(
    analysis_job_id: str,
    status: str,
    *,
    error_message: str = "",
    row_count: Optional[int] = None,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
) -> FactorAnalysisJobOut:
    current = get_analysis_job(analysis_job_id)
    if not current:
        raise ValueError(f"分析任务不存在: {analysis_job_id}")
    now = datetime.now()
    database = settings().clickhouse_database
    row = [
        current.analysis_job_id,
        current.factor_id,
        current.factor_version,
        current.entity_type,
        current.params_hash,
        current.date_start,
        current.date_end,
        current.periods,
        current.quantiles,
        current.price_field,
        1 if current.cumulative_returns else 0,
        current.max_loss,
        status,
        error_message,
        row_count,
        current.created_at or now,
        started_at if started_at is not None else current.started_at,
        finished_at if finished_at is not None else current.finished_at,
        now,
    ]
    client().insert(
        f"{database}.factor_analysis_jobs",
        [row],
        column_names=[
            "analysis_job_id",
            "factor_id",
            "factor_version",
            "entity_type",
            "params_hash",
            "date_start",
            "date_end",
            "periods",
            "quantiles",
            "price_field",
            "cumulative_returns",
            "max_loss",
            "status",
            "error_message",
            "row_count",
            "created_at",
            "started_at",
            "finished_at",
            "updated_at",
        ],
    )
    updated = get_analysis_job(analysis_job_id)
    if not updated:
        raise RuntimeError("分析任务状态更新后读取失败")
    return updated


def replace_analysis_results(
    analysis_job_id: str,
    *,
    summary_rows: list[tuple],
    ic_rows: list[tuple],
    quantile_return_rows: list[tuple],
    turnover_rows: list[tuple],
) -> None:
    database = settings().clickhouse_database
    now = datetime.now()
    _delete_analysis_rows(database, analysis_job_id)
    if summary_rows:
        client().insert(
            f"{database}.factor_analysis_summary",
            [list(row) + [now] for row in summary_rows],
            column_names=[
                "analysis_job_id",
                "metric",
                "period",
                "value",
                "payload_json",
                "updated_at",
            ],
        )
    if ic_rows:
        client().insert(
            f"{database}.factor_analysis_ic_daily",
            [list(row) + [now] for row in ic_rows],
            column_names=["analysis_job_id", "trade_date", "period", "ic", "updated_at"],
        )
    if quantile_return_rows:
        client().insert(
            f"{database}.factor_analysis_quantile_returns",
            [list(row) + [now] for row in quantile_return_rows],
            column_names=[
                "analysis_job_id",
                "trade_date",
                "period",
                "quantile",
                "mean_return",
                "updated_at",
            ],
        )
    if turnover_rows:
        client().insert(
            f"{database}.factor_analysis_turnover_daily",
            [list(row) + [now] for row in turnover_rows],
            column_names=[
                "analysis_job_id",
                "trade_date",
                "period",
                "quantile",
                "turnover",
                "rank_autocorrelation",
                "updated_at",
            ],
        )


def list_analysis_summary(analysis_job_id: str) -> list[FactorAnalysisSummaryOut]:
    database = settings().clickhouse_database
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.factor_analysis_summary FINAL
        WHERE analysis_job_id = {{analysis_job_id:String}}
        ORDER BY metric ASC, period ASC
        """,
        parameters={"analysis_job_id": analysis_job_id},
    ).result_rows
    return [_analysis_summary_from_row(row) for row in rows]


def list_analysis_ic(
    analysis_job_id: str,
    period: Optional[str] = None,
    limit: int = 1000,
) -> list[FactorAnalysisIcOut]:
    database = settings().clickhouse_database
    conditions = ["analysis_job_id = {analysis_job_id:String}"]
    params = {"analysis_job_id": analysis_job_id, "limit": max(1, min(limit, 5000))}
    if period:
        conditions.append("period = {period:String}")
        params["period"] = period
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.factor_analysis_ic_daily FINAL
        WHERE {' AND '.join(conditions)}
        ORDER BY trade_date ASC, period ASC
        LIMIT {{limit:UInt32}}
        """,
        parameters=params,
    ).result_rows
    return [_analysis_ic_from_row(row) for row in rows]


def list_analysis_quantile_returns(
    analysis_job_id: str,
    period: Optional[str] = None,
    limit: int = 5000,
) -> list[FactorAnalysisQuantileReturnOut]:
    database = settings().clickhouse_database
    conditions = ["analysis_job_id = {analysis_job_id:String}"]
    params = {"analysis_job_id": analysis_job_id, "limit": max(1, min(limit, 20000))}
    if period:
        conditions.append("period = {period:String}")
        params["period"] = period
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.factor_analysis_quantile_returns FINAL
        WHERE {' AND '.join(conditions)}
        ORDER BY trade_date ASC, period ASC, quantile ASC
        LIMIT {{limit:UInt32}}
        """,
        parameters=params,
    ).result_rows
    return [_analysis_quantile_return_from_row(row) for row in rows]


def list_analysis_turnover(
    analysis_job_id: str,
    period: Optional[str] = None,
    limit: int = 5000,
) -> list[FactorAnalysisTurnoverOut]:
    database = settings().clickhouse_database
    conditions = ["analysis_job_id = {analysis_job_id:String}"]
    params = {"analysis_job_id": analysis_job_id, "limit": max(1, min(limit, 20000))}
    if period:
        conditions.append("period = {period:String}")
        params["period"] = period
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.factor_analysis_turnover_daily FINAL
        WHERE {' AND '.join(conditions)}
        ORDER BY trade_date ASC, period ASC, quantile ASC
        LIMIT {{limit:UInt32}}
        """,
        parameters=params,
    ).result_rows
    return [_analysis_turnover_from_row(row) for row in rows]


def list_values(
    factor_id: Optional[str] = None,
    factor_version: Optional[int] = None,
    entity_type: Optional[str] = None,
    entity_code: Optional[str] = None,
    entity_codes: Optional[list[str]] = None,
    params_hash: Optional[str] = None,
    job_id: Optional[str] = None,
    trade_date: Optional[date] = None,
    date_start: Optional[date] = None,
    date_end: Optional[date] = None,
    date_end_exclusive: Optional[date] = None,
    available_before: Optional[datetime] = None,
    limit: int = 500,
    offset: int = 0,
    order_by: str = "trade_date",
    order_dir: str = "desc",
) -> list[FactorValueOut]:
    database = settings().clickhouse_database
    conditions, params = _value_conditions(
        factor_id=factor_id,
        factor_version=factor_version,
        entity_type=entity_type,
        entity_code=entity_code,
        entity_codes=entity_codes,
        params_hash=params_hash,
        job_id=job_id,
        trade_date=trade_date,
        date_start=date_start,
        date_end=date_end,
        date_end_exclusive=date_end_exclusive,
        available_before=available_before,
    )
    params["limit"] = max(1, min(limit, 5000))
    params["offset"] = max(0, offset)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    order_column = _value_order_column(order_by)
    direction = "ASC" if str(order_dir).lower() == "asc" else "DESC"
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.factor_values_daily
        {where}
        ORDER BY {order_column} {direction}, trade_date DESC, entity_code ASC
        LIMIT {{limit:UInt32}}
        OFFSET {{offset:UInt32}}
        """,
        parameters=params,
    ).result_rows
    return [_value_from_row(row) for row in rows]


def count_values(
    factor_id: Optional[str] = None,
    factor_version: Optional[int] = None,
    entity_type: Optional[str] = None,
    entity_code: Optional[str] = None,
    entity_codes: Optional[list[str]] = None,
    params_hash: Optional[str] = None,
    job_id: Optional[str] = None,
    trade_date: Optional[date] = None,
    date_start: Optional[date] = None,
    date_end: Optional[date] = None,
    date_end_exclusive: Optional[date] = None,
    available_before: Optional[datetime] = None,
) -> int:
    database = settings().clickhouse_database
    conditions, params = _value_conditions(
        factor_id=factor_id,
        factor_version=factor_version,
        entity_type=entity_type,
        entity_code=entity_code,
        entity_codes=entity_codes,
        params_hash=params_hash,
        job_id=job_id,
        trade_date=trade_date,
        date_start=date_start,
        date_end=date_end,
        date_end_exclusive=date_end_exclusive,
        available_before=available_before,
    )
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = client().query(
        f"""
        SELECT count()
        FROM {database}.factor_values_daily
        {where}
        """,
        parameters=params,
    ).result_rows
    return int(rows[0][0] or 0)


def latest_value_date(
    factor_id: str,
    factor_version: Optional[int] = None,
    entity_type: Optional[str] = None,
    params_hash: Optional[str] = None,
    job_id: Optional[str] = None,
    date_start: Optional[date] = None,
    date_end: Optional[date] = None,
) -> Optional[date]:
    database = settings().clickhouse_database
    conditions, params = _value_conditions(
        factor_id=factor_id,
        factor_version=factor_version,
        entity_type=entity_type,
        params_hash=params_hash,
        job_id=job_id,
        date_start=date_start,
        date_end=date_end,
    )
    rows = client().query(
        f"""
        SELECT count(), max(trade_date)
        FROM {database}.factor_values_daily
        WHERE {' AND '.join(conditions)}
        """,
        parameters=params,
    ).result_rows
    if not rows or int(rows[0][0] or 0) == 0:
        return None
    value = rows[0][1]
    return value or None


def coverage(
    factor_id: str,
    factor_version: Optional[int] = None,
    entity_type: Optional[str] = None,
    params_hash: Optional[str] = None,
    job_id: Optional[str] = None,
    date_start: Optional[date] = None,
    date_end: Optional[date] = None,
) -> CoverageOut:
    database = settings().clickhouse_database
    conditions, params = _value_conditions(
        factor_id=factor_id,
        factor_version=factor_version,
        entity_type=entity_type,
        params_hash=params_hash,
        job_id=job_id,
        date_start=date_start,
        date_end=date_end,
    )
    rows = client().query(
        f"""
        SELECT
            count() AS rows,
            uniqExact(entity_code) AS entity_count,
            uniqExact(trade_date) AS trade_date_count
        FROM {database}.factor_values_daily
        WHERE {' AND '.join(conditions)}
        """,
        parameters=params,
    ).result_rows
    row = rows[0] if rows else (0, 0, 0)
    return CoverageOut(
        factor_id=factor_id,
        date_start=date_start,
        date_end=date_end,
        rows=int(row[0] or 0),
        entity_count=int(row[1] or 0),
        trade_date_count=int(row[2] or 0),
    )


def value_quality(
    factor_id: str,
    factor_version: Optional[int] = None,
    entity_type: Optional[str] = None,
    params_hash: Optional[str] = None,
    job_id: Optional[str] = None,
    date_start: Optional[date] = None,
    date_end: Optional[date] = None,
) -> FactorValueQualityOut:
    database = settings().clickhouse_database
    conditions, params = _value_conditions(
        factor_id=factor_id,
        factor_version=factor_version,
        entity_type=entity_type,
        params_hash=params_hash,
        job_id=job_id,
        date_start=date_start,
        date_end=date_end,
    )
    rows = client().query(
        f"""
        SELECT
            min(trade_date) AS date_start,
            max(trade_date) AS date_end,
            count() AS rows,
            uniqExact(entity_code) AS entity_count,
            uniqExact(trade_date) AS trade_date_count,
            max(updated_at) AS latest_updated_at,
            count(raw_value) AS raw_count,
            countIf(isNull(raw_value)) AS raw_null_count,
            countIf(raw_value = 0) AS raw_zero_count,
            min(raw_value) AS raw_min,
            max(raw_value) AS raw_max,
            avg(raw_value) AS raw_avg,
            stddevPop(raw_value) AS raw_stddev,
            count(rank_value) AS rank_count,
            countIf(isNull(rank_value)) AS rank_null_count,
            countIf(rank_value = 0) AS rank_zero_count,
            min(rank_value) AS rank_min,
            max(rank_value) AS rank_max,
            avg(rank_value) AS rank_avg,
            stddevPop(rank_value) AS rank_stddev,
            count(percentile) AS percentile_count,
            countIf(isNull(percentile)) AS percentile_null_count,
            countIf(percentile = 0) AS percentile_zero_count,
            min(percentile) AS percentile_min,
            max(percentile) AS percentile_max,
            avg(percentile) AS percentile_avg,
            stddevPop(percentile) AS percentile_stddev,
            count(score) AS score_count,
            countIf(isNull(score)) AS score_null_count,
            countIf(score = 0) AS score_zero_count,
            min(score) AS score_min,
            max(score) AS score_max,
            avg(score) AS score_avg,
            stddevPop(score) AS score_stddev
        FROM {database}.factor_values_daily
        WHERE {' AND '.join(conditions)}
        """,
        parameters=params,
    ).result_rows
    row = rows[0] if rows else None
    total_rows = int(row[2] or 0) if row else 0
    metric_specs = [
        ("raw_value", 6),
        ("rank_value", 13),
        ("percentile", 20),
        ("score", 27),
    ]
    metrics = {
        field: _metric_quality_from_row(field, total_rows, row, offset)
        for field, offset in metric_specs
    }
    postprocess_fields = ["rank_value", "percentile", "score"]
    postprocess_counts = [metrics[field].count for field in postprocess_fields]
    if not total_rows:
        postprocess_status = "empty"
    elif all(count == 0 for count in postprocess_counts):
        postprocess_status = "not_generated"
    elif all(count == total_rows for count in postprocess_counts):
        postprocess_status = "generated"
    else:
        postprocess_status = "partial"

    warnings = []
    raw_metric = metrics["raw_value"]
    if not total_rows:
        warnings.append("当前筛选条件下没有因子结果")
    elif raw_metric.all_null:
        warnings.append("原始值全部为空，公式或数据源可能没有产出有效值")
    elif raw_metric.all_zero:
        warnings.append("原始值全部为 0，需要检查公式逻辑或基础字段")
    if total_rows and postprocess_status == "not_generated":
        warnings.append("rank_value / percentile / score 均未生成，去极值、中性化、标准化还没有实际落库")
    elif postprocess_status == "partial":
        warnings.append("后处理列只生成了部分数据，需要检查计算任务是否完整")

    return FactorValueQualityOut(
        factor_id=factor_id,
        factor_version=factor_version,
        entity_type=entity_type,
        params_hash=params_hash or "",
        job_id=job_id or "",
        date_start=row[0] if total_rows and row else None,
        date_end=row[1] if total_rows and row else None,
        rows=total_rows,
        entity_count=int(row[3] or 0) if row else 0,
        trade_date_count=int(row[4] or 0) if row else 0,
        latest_updated_at=row[5] if total_rows and row else None,
        metrics=metrics,
        postprocess_status=postprocess_status,
        warnings=warnings,
    )


def _value_conditions(
    factor_id: Optional[str] = None,
    factor_version: Optional[int] = None,
    entity_type: Optional[str] = None,
    entity_code: Optional[str] = None,
    entity_codes: Optional[list[str]] = None,
    params_hash: Optional[str] = None,
    job_id: Optional[str] = None,
    trade_date: Optional[date] = None,
    date_start: Optional[date] = None,
    date_end: Optional[date] = None,
    date_end_exclusive: Optional[date] = None,
    available_before: Optional[datetime] = None,
) -> tuple[list[str], dict]:
    conditions = []
    params = {}
    if factor_id:
        conditions.append("factor_id = {factor_id:String}")
        params["factor_id"] = factor_id
        if factor_version:
            conditions.append("factor_version = {factor_version:UInt32}")
            params["factor_version"] = int(factor_version)
        else:
            database = settings().clickhouse_database
            conditions.append(
                f"factor_version = (SELECT max(version) FROM {database}.factor_definitions WHERE factor_id = {{factor_id:String}})"
            )
    if entity_type:
        conditions.append("entity_type = {entity_type:String}")
        params["entity_type"] = entity_type
    if entity_code:
        conditions.append("entity_code = {entity_code:String}")
        params["entity_code"] = entity_code
    if entity_codes:
        cleaned_codes = sorted(
            {str(item).strip() for item in entity_codes if str(item).strip()}
        )
        if cleaned_codes:
            conditions.append("entity_code IN {entity_codes:Array(String)}")
            params["entity_codes"] = cleaned_codes
    if params_hash:
        conditions.append("params_hash = {params_hash:String}")
        params["params_hash"] = params_hash
    if job_id:
        conditions.append("job_id = {job_id:String}")
        params["job_id"] = job_id
    if trade_date:
        conditions.append("trade_date = {trade_date:Date}")
        params["trade_date"] = trade_date
    if date_start:
        conditions.append("trade_date >= {date_start:Date}")
        params["date_start"] = date_start
    if date_end:
        conditions.append("trade_date <= {date_end:Date}")
        params["date_end"] = date_end
    if date_end_exclusive:
        conditions.append("trade_date < {date_end_exclusive:Date}")
        params["date_end_exclusive"] = date_end_exclusive
    if available_before:
        conditions.append("available_at IS NOT NULL")
        conditions.append("available_at <= {available_before:DateTime}")
        params["available_before"] = available_before
    return conditions, params


def _metric_quality_from_row(field: str, total_rows: int, row, offset: int) -> FactorValueMetricQualityOut:
    if not row:
        return FactorValueMetricQualityOut(
            field=field,
            rows=0,
            count=0,
            null_count=0,
            zero_count=0,
            all_null=True,
            all_zero=False,
        )
    count_value = int(row[offset] or 0)
    null_count = int(row[offset + 1] or 0)
    zero_count = int(row[offset + 2] or 0)
    return FactorValueMetricQualityOut(
        field=field,
        rows=total_rows,
        count=count_value,
        null_count=null_count,
        zero_count=zero_count,
        null_ratio=(null_count / total_rows) if total_rows else 0,
        zero_ratio=(zero_count / count_value) if count_value else 0,
        min=_nullable_float(row[offset + 3]),
        max=_nullable_float(row[offset + 4]),
        avg=_nullable_float(row[offset + 5]),
        stddev=_nullable_float(row[offset + 6]),
        all_null=total_rows > 0 and null_count == total_rows,
        all_zero=count_value > 0 and zero_count == count_value,
    )


def _nullable_float(value) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _value_order_column(value: str) -> str:
    columns = {
        "trade_date": "trade_date",
        "entity_code": "entity_code",
        "asset_code": "entity_code",
        "factor_id": "factor_id",
        "raw_value": "raw_value",
        "rank_value": "rank_value",
        "percentile": "percentile",
        "score": "score",
        "updated_at": "updated_at",
    }
    return columns.get(str(value or "").strip(), "trade_date")


def _clean_periods(periods: list[int]) -> list[int]:
    cleaned = sorted({int(item) for item in periods if int(item) > 0})
    if not cleaned:
        raise ValueError("分析周期不能为空")
    return cleaned


def _delete_analysis_rows(database: str, analysis_job_id: str) -> None:
    params = {"analysis_job_id": analysis_job_id}
    for table in (
        "factor_analysis_summary",
        "factor_analysis_ic_daily",
        "factor_analysis_quantile_returns",
        "factor_analysis_turnover_daily",
    ):
        client().command(
            f"ALTER TABLE {database}.{table} DELETE WHERE analysis_job_id = {{analysis_job_id:String}}",
            parameters=params,
        )


def _insert_factor(payload: FactorCreate, version: int, created_at: datetime, updated_at: datetime) -> None:
    database = settings().clickhouse_database
    row = [
        payload.factor_id,
        version,
        payload.label,
        payload.description,
        payload.entity_type,
        payload.category,
        payload.group_name,
        payload.output_type,
        payload.frequency,
        payload.asset_id,
        payload.source_node_id,
        payload.required_fields,
        json.dumps(payload.params, ensure_ascii=False, sort_keys=True),
        json.dumps(
            payload.param_schema,
            ensure_ascii=False,
            sort_keys=True,
        ),
        json.dumps(
            payload.availability_policy,
            ensure_ascii=False,
            sort_keys=True,
        ),
        payload.expression,
        1 if payload.enabled else 0,
        created_at,
        updated_at,
    ]
    client().insert(
        f"{database}.factor_definitions",
        [row],
        column_names=[
            "factor_id",
            "version",
            "label",
            "description",
            "entity_type",
            "category",
            "group_name",
            "output_type",
            "frequency",
            "asset_id",
            "source_node_id",
            "required_fields",
            "params_json",
            "param_schema_json",
            "availability_policy_json",
            "expression",
            "enabled",
            "created_at",
            "updated_at",
        ],
    )


def _json_dict(value: str) -> dict:
    try:
        loaded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _inferred_param_schema(
    params: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    schema: dict[str, dict[str, Any]] = {}
    for name, value in sorted(params.items()):
        if isinstance(value, bool):
            spec: dict[str, Any] = {"type": "boolean"}
        elif isinstance(value, int):
            spec = {"type": "integer"}
        elif isinstance(value, float) and math.isfinite(value):
            spec = {"type": "number"}
        elif isinstance(value, str):
            spec = {"type": "string"}
        else:
            raise ValueError(f"参数 {name} 缺少可推导的严格类型")
        clean_name = str(name)
        lowered = clean_name.lower()
        if spec["type"] == "integer" and (
            "window" in lowered or "period" in lowered
        ):
            spec.update({"minimum": 1, "maximum": 10000})
        elif spec["type"] == "number" and lowered.endswith("_weight"):
            spec.update({"minimum": 0, "maximum": 1})
        spec["default"] = value
        schema[clean_name] = spec
    return schema


def _validated_param_schema(
    schema: dict[str, dict[str, Any]],
    defaults: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    if set(schema) != set(defaults):
        raise ValueError("param_schema 必须精确覆盖全部默认参数")
    allowed_types = {"boolean", "integer", "number", "string"}
    normalized: dict[str, dict[str, Any]] = {}
    for name, raw in schema.items():
        if not isinstance(raw, dict):
            raise ValueError(f"参数 {name} 的 schema 必须是对象")
        spec = dict(raw)
        dtype = str(spec.get("type") or "").strip().lower()
        if dtype not in allowed_types:
            raise ValueError(f"参数 {name} 的 schema type 不受支持")
        spec["type"] = dtype
        if "default" in spec and spec["default"] != defaults[name]:
            raise ValueError(f"参数 {name} 的 schema default 与 params 不一致")
        spec["default"] = defaults[name]
        _validate_parameter_value(str(name), defaults[name], spec)
        normalized[str(name)] = spec
    return normalized


def _normalize_factor_params(
    schema: dict[str, dict[str, Any]],
    defaults: dict[str, Any],
    supplied: dict[str, Any],
) -> dict[str, Any]:
    unknown = sorted(set(supplied) - set(schema))
    if unknown:
        raise ValueError("任务包含未声明参数: " + ", ".join(unknown))
    normalized: dict[str, Any] = {}
    for name, spec in schema.items():
        if name not in supplied and name not in defaults:
            if bool(spec.get("required")):
                raise ValueError(f"任务缺少必需参数: {name}")
            continue
        value = supplied[name] if name in supplied else defaults[name]
        _validate_parameter_value(name, value, spec)
        normalized[name] = value
    return normalized


def _validate_parameter_value(
    name: str,
    value: Any,
    spec: dict[str, Any],
) -> None:
    dtype = str(spec.get("type") or "").strip().lower()
    valid = (
        type(value) is bool
        if dtype == "boolean"
        else type(value) is int
        if dtype == "integer"
        else (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
        if dtype == "number"
        else isinstance(value, str)
        if dtype == "string"
        else False
    )
    if not valid:
        raise ValueError(f"参数 {name} 必须是 {dtype}")
    minimum = spec.get("minimum")
    maximum = spec.get("maximum")
    choices = list(spec.get("choices", spec.get("enum", ())) or ())
    if minimum is not None and value < minimum:
        raise ValueError(f"参数 {name} 小于 minimum")
    if maximum is not None and value > maximum:
        raise ValueError(f"参数 {name} 大于 maximum")
    if choices and value not in choices:
        raise ValueError(f"参数 {name} 不在允许值中")


def _definition_hash(payload: dict[str, Any]) -> str:
    stable = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "definition_hash",
            "available_versions",
            "created_at",
            "updated_at",
        }
    }
    return hashlib.sha256(
        json.dumps(
            stable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _factor_from_row(
    row,
    *,
    available_versions: tuple[int, ...] = (),
) -> FactorOut:
    params = _json_dict(row[12])
    param_schema = _json_dict(row[13]) or _inferred_param_schema(params)
    availability_policy = _json_dict(row[14]) or {
        "field": "available_at",
        "policy": "persisted_timestamp",
    }
    payload = {
        "factor_id": row[0],
        "version": int(row[1]),
        "label": row[2],
        "description": row[3],
        "entity_type": row[4],
        "category": row[5],
        "group_name": row[6],
        "output_type": row[7],
        "frequency": row[8],
        "asset_id": row[9],
        "source_node_id": row[10],
        "required_fields": list(row[11] or []),
        "params": params,
        "param_schema": param_schema,
        "availability_policy": availability_policy,
        "expression": row[15],
        "enabled": bool(row[16]),
        "available_versions": list(available_versions),
        "created_at": row[17],
        "updated_at": row[18],
    }
    payload["definition_hash"] = _definition_hash(payload)
    return FactorOut(**payload)


def _job_from_row(row) -> FactorJobOut:
    return FactorJobOut(
        job_id=row[0],
        factor_id=row[1],
        factor_version=int(row[2]),
        entity_type=row[3],
        mode=row[4],
        universe=row[5],
        date_start=row[6],
        date_end=row[7],
        params=_json_dict(row[8]),
        status=row[9],
        error_message=row[10],
        row_count=row[11],
        created_at=row[12],
        started_at=row[13],
        finished_at=row[14],
        updated_at=row[15],
    )


def _value_from_row(row) -> FactorValueOut:
    return FactorValueOut(
        trade_date=row[0],
        entity_type=row[1],
        entity_code=row[2],
        factor_id=row[3],
        factor_version=int(row[4]),
        params_hash=row[5],
        raw_value=row[6],
        rank_value=row[7],
        percentile=row[8],
        score=row[9],
        job_id=row[10],
        available_at=row[11],
        updated_at=row[12],
    )


def _analysis_job_from_row(row) -> FactorAnalysisJobOut:
    return FactorAnalysisJobOut(
        analysis_job_id=row[0],
        factor_id=row[1],
        factor_version=int(row[2]),
        entity_type=row[3],
        params_hash=row[4],
        date_start=row[5],
        date_end=row[6],
        periods=[int(item) for item in (row[7] or [])],
        quantiles=int(row[8]),
        price_field=row[9],
        cumulative_returns=bool(row[10]),
        max_loss=float(row[11]),
        status=row[12],
        error_message=row[13],
        row_count=row[14],
        created_at=row[15],
        started_at=row[16],
        finished_at=row[17],
        updated_at=row[18],
    )


def _analysis_summary_from_row(row) -> FactorAnalysisSummaryOut:
    return FactorAnalysisSummaryOut(
        analysis_job_id=row[0],
        metric=row[1],
        period=row[2],
        value=row[3],
        payload=_json_dict(row[4]),
        updated_at=row[5],
    )


def _analysis_ic_from_row(row) -> FactorAnalysisIcOut:
    return FactorAnalysisIcOut(
        analysis_job_id=row[0],
        trade_date=row[1],
        period=row[2],
        ic=row[3],
        updated_at=row[4],
    )


def _analysis_quantile_return_from_row(row) -> FactorAnalysisQuantileReturnOut:
    return FactorAnalysisQuantileReturnOut(
        analysis_job_id=row[0],
        trade_date=row[1],
        period=row[2],
        quantile=int(row[3]),
        mean_return=row[4],
        updated_at=row[5],
    )


def _analysis_turnover_from_row(row) -> FactorAnalysisTurnoverOut:
    return FactorAnalysisTurnoverOut(
        analysis_job_id=row[0],
        trade_date=row[1],
        period=row[2],
        quantile=int(row[3]),
        turnover=row[4],
        rank_autocorrelation=row[5],
        updated_at=row[6],
    )
