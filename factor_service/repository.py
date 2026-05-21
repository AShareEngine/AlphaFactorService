from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional
from uuid import uuid4

from factor_service.clickhouse import client, settings
from factor_service.schemas import (
    CoverageOut,
    FactorCreate,
    FactorJobCreate,
    FactorJobOut,
    FactorOut,
    FactorUpdate,
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
    return [_factor_from_row(row) for row in rows]


def get_factor(factor_id: str) -> Optional[FactorOut]:
    database = settings().clickhouse_database
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.factor_definitions
        WHERE factor_id = {{factor_id:String}}
        ORDER BY version DESC
        LIMIT 1
        """,
        parameters={"factor_id": factor_id},
    ).result_rows
    return _factor_from_row(rows[0]) if rows else None


def create_factor(payload: FactorCreate) -> FactorOut:
    existing = get_factor(payload.factor_id)
    if existing:
        raise ValueError(f"因子已存在: {payload.factor_id}")
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


def create_job(payload: FactorJobCreate) -> FactorJobOut:
    factor = get_factor(payload.factor_id)
    if not factor:
        raise ValueError(f"因子不存在: {payload.factor_id}")
    version = payload.factor_version or factor.version
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


def list_values(
    factor_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_code: Optional[str] = None,
    trade_date: Optional[date] = None,
    date_start: Optional[date] = None,
    date_end: Optional[date] = None,
    limit: int = 500,
) -> list[FactorValueOut]:
    database = settings().clickhouse_database
    conditions = []
    params = {"limit": max(1, min(limit, 5000))}
    if factor_id:
        conditions.append("factor_id = {factor_id:String}")
        params["factor_id"] = factor_id
    if entity_type:
        conditions.append("entity_type = {entity_type:String}")
        params["entity_type"] = entity_type
    if entity_code:
        conditions.append("entity_code = {entity_code:String}")
        params["entity_code"] = entity_code
    if trade_date:
        conditions.append("trade_date = {trade_date:Date}")
        params["trade_date"] = trade_date
    if date_start:
        conditions.append("trade_date >= {date_start:Date}")
        params["date_start"] = date_start
    if date_end:
        conditions.append("trade_date <= {date_end:Date}")
        params["date_end"] = date_end
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.factor_values_daily
        {where}
        ORDER BY trade_date DESC, factor_id ASC, entity_code ASC
        LIMIT {{limit:UInt32}}
        """,
        parameters=params,
    ).result_rows
    return [_value_from_row(row) for row in rows]


def coverage(
    factor_id: str,
    date_start: Optional[date] = None,
    date_end: Optional[date] = None,
) -> CoverageOut:
    database = settings().clickhouse_database
    conditions = ["factor_id = {factor_id:String}"]
    params = {"factor_id": factor_id}
    if date_start:
        conditions.append("trade_date >= {date_start:Date}")
        params["date_start"] = date_start
    if date_end:
        conditions.append("trade_date <= {date_end:Date}")
        params["date_end"] = date_end
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
        payload.required_fields,
        json.dumps(payload.params, ensure_ascii=False, sort_keys=True),
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
            "required_fields",
            "params_json",
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


def _factor_from_row(row) -> FactorOut:
    return FactorOut(
        factor_id=row[0],
        version=int(row[1]),
        label=row[2],
        description=row[3],
        entity_type=row[4],
        category=row[5],
        group_name=row[6],
        output_type=row[7],
        frequency=row[8],
        required_fields=list(row[9] or []),
        params=_json_dict(row[10]),
        expression=row[11],
        enabled=bool(row[12]),
        created_at=row[13],
        updated_at=row[14],
    )


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
        updated_at=row[11],
    )
