from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from factor_service import repository
from factor_service.clickhouse import client, settings
from factor_service.qlib_formula import compile_qlib_formula
from factor_service.schemas import FactorJobOut, FactorOut


logger = logging.getLogger(__name__)

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ComputePlan:
    sql: str
    params: dict
    date_start: date
    date_end: date
    params_hash: str


@dataclass(frozen=True)
class ValueSqlPlan:
    sql: str
    max_window: int


def run_pending_jobs(limit: int = 5) -> list[FactorJobOut]:
    jobs = repository.list_jobs(status="pending", limit=limit)
    return [run_job(job.job_id) for job in jobs]


def run_job(job_id: str) -> FactorJobOut:
    job = repository.get_job(job_id)
    if not job:
        raise ValueError(f"任务不存在: {job_id}")
    if job.status == "success":
        return job
    if job.status == "running":
        return job

    started_at = datetime.now()
    repository.update_job_status(job.job_id, "running", started_at=started_at)
    try:
        factor = repository.get_factor(job.factor_id)
        if not factor:
            raise ValueError(f"因子不存在: {job.factor_id}")
        if not factor.enabled:
            raise ValueError(f"因子已停用: {job.factor_id}")
        plan = build_compute_plan(factor, job)
        client().command(plan.sql, parameters=plan.params)
        row_count = _count_job_values(job.job_id)
        return repository.update_job_status(
            job.job_id,
            "success",
            row_count=row_count,
            started_at=started_at,
            finished_at=datetime.now(),
        )
    except Exception as exc:
        logger.exception("factor job failed: %s", job.job_id)
        return repository.update_job_status(
            job.job_id,
            "failed",
            error_message=str(exc),
            started_at=started_at,
            finished_at=datetime.now(),
        )


def build_compute_plan(factor: FactorOut, job: FactorJobOut) -> ComputePlan:
    config = settings()
    factor_db = _identifier(config.clickhouse_database, "factor database")
    source_db = _identifier(config.source_database, "source database")
    source_table = _identifier(config.stock_daily_table, "stock daily table")
    stock_basic_table = _identifier(config.stock_basic_table, "stock basic table")
    code_column = _identifier(config.stock_code_column, "stock code column")
    date_column = _identifier(config.stock_date_column, "stock date column")
    stock_type_column = _identifier(config.stock_basic_type_column, "stock basic type column")

    params = dict(factor.params)
    params.update(job.params or {})
    window = _positive_int(params.get("window", 20), "window")
    source = f"{source_db}.{source_table}"
    stock_basic = f"{source_db}.{stock_basic_table}"
    universe_filter = f"""
        AND {code_column} IN (
            SELECT {code_column}
            FROM {stock_basic}
            WHERE {stock_type_column} = {{stock_type_value:String}}
        )
    """
    value_plan = _build_value_sql(
        factor.expression,
        source=source,
        code_column=code_column,
        date_column=date_column,
        params={**params, "window": window},
        universe_filter=universe_filter,
    )
    date_start, date_end = _resolve_date_range(job.date_start, job.date_end, source_db, source_table, date_column)
    lookback_days = max(value_plan.max_window * 4 + 20, 90)
    source_start = date_start - timedelta(days=lookback_days)
    params_hash = _params_hash(factor.factor_id, factor.version, params)
    base_params = {
        "date_start": date_start,
        "date_end": date_end,
        "source_start": source_start,
        "entity_type": job.entity_type,
        "factor_id": factor.factor_id,
        "factor_version": factor.version,
        "params_hash": params_hash,
        "job_id": job.job_id,
        "stock_type_value": config.stock_basic_stock_type_value,
    }
    sql = f"""
    INSERT INTO {factor_db}.factor_values_daily
    (
        trade_date,
        entity_type,
        entity_code,
        factor_id,
        factor_version,
        params_hash,
        raw_value,
        rank_value,
        percentile,
        score,
        job_id,
        updated_at
    )
    SELECT
        trade_date,
        {{entity_type:String}},
        entity_code,
        {{factor_id:String}},
        {{factor_version:UInt32}},
        {{params_hash:String}},
        raw_value,
        NULL,
        NULL,
        NULL,
        {{job_id:String}},
        now()
    FROM (
        {value_plan.sql}
    )
    WHERE trade_date >= {{date_start:Date}}
      AND trade_date <= {{date_end:Date}}
      AND raw_value IS NOT NULL
    """
    return ComputePlan(sql=sql, params=base_params, date_start=date_start, date_end=date_end, params_hash=params_hash)


def _build_value_sql(
    expression: str,
    *,
    source: str,
    code_column: str,
    date_column: str,
    params: dict,
    universe_filter: str,
) -> ValueSqlPlan:
    compiled = compile_qlib_formula(
        expression,
        params=params,
        code_column=code_column,
        date_column=date_column,
    )
    return ValueSqlPlan(sql=f"""
    SELECT
        {date_column} AS trade_date,
        {code_column} AS entity_code,
        {compiled.sql} AS raw_value
    FROM {source}
    WHERE {date_column} >= {{source_start:Date}}
      AND {date_column} <= {{date_end:Date}}
      {universe_filter}
    """, max_window=compiled.max_window)


def _resolve_date_range(
    date_start: Optional[date],
    date_end: Optional[date],
    source_db: str,
    source_table: str,
    date_column: str,
) -> tuple[date, date]:
    if date_end is None:
        rows = client().query(f"SELECT max({date_column}) FROM {source_db}.{source_table}").result_rows
        date_end = rows[0][0] if rows else None
    if date_start is None:
        date_start = date_end
    if date_start is None or date_end is None:
        raise ValueError("缺少计算日期范围，且源表没有可用日期")
    if date_start > date_end:
        raise ValueError("开始日期不能晚于结束日期")
    return date_start, date_end


def _count_job_values(job_id: str) -> int:
    database = _identifier(settings().clickhouse_database, "factor database")
    rows = client().query(
        f"SELECT count() FROM {database}.factor_values_daily WHERE job_id = {{job_id:String}}",
        parameters={"job_id": job_id},
    ).result_rows
    return int(rows[0][0] or 0)


def _params_hash(factor_id: str, version: int, params: dict) -> str:
    payload = json.dumps(
        {"factor_id": factor_id, "version": version, "params": params},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.match(value or ""):
        raise ValueError(f"{label} 不是合法标识: {value}")
    return value


def _positive_int(value, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是正整数") from exc
    if parsed <= 0:
        raise ValueError(f"{label} 必须是正整数")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Run AlphaFactorService compute jobs.")
    parser.add_argument("--job-id", default="", help="Run one job by id.")
    parser.add_argument("--limit", type=int, default=5, help="Max pending jobs per polling cycle.")
    parser.add_argument("--poll-interval", type=int, default=60, help="Seconds between polling cycles.")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.job_id:
        job = run_job(args.job_id)
        logger.info("job %s finished with status=%s rows=%s", job.job_id, job.status, job.row_count)
        return

    while True:
        jobs = run_pending_jobs(limit=max(1, args.limit))
        if jobs:
            logger.info("processed %s pending factor jobs", len(jobs))
        if args.once:
            return
        time.sleep(max(5, args.poll_interval))


if __name__ == "__main__":
    main()
