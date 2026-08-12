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
    fields: list[str]


@dataclass(frozen=True)
class PostprocessConfig:
    winsorize: str
    standardize: str
    neutralize: tuple[str, ...]


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
        factor = repository.get_factor(
            job.factor_id,
            version=job.factor_version,
        )
        if not factor:
            raise ValueError(f"因子不存在: {job.factor_id}")
        if not factor.enabled:
            raise ValueError(f"因子已停用: {job.factor_id}")
        plan = build_compute_plan(factor, job)
        client().command(plan.sql, parameters=plan.params)
        _cleanup_superseded_values(plan)
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
    if factor.frequency != "daily":
        raise ValueError(
            "分钟因子必须由分钟计算器写入，daily worker 不接受 minute 因子"
        )
    config = settings()
    factor_db = _identifier(config.clickhouse_database, "factor database")
    source_db = _identifier(config.source_database, "source database")
    source_table = _identifier(config.stock_daily_table, "stock daily table")
    stock_basic_table = _identifier(config.stock_basic_table, "stock basic table")
    code_column = _identifier(config.stock_code_column, "stock code column")
    date_column = _identifier(config.stock_date_column, "stock date column")
    stock_type_column = _identifier(config.stock_basic_type_column, "stock basic type column")

    params = _formula_params(factor, job.params or {})
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
    processing = _postprocess_config(factor)
    postprocessed_sql = _build_postprocessed_sql(
        value_plan.sql,
        output_type=factor.output_type,
        processing=processing,
    )
    date_start, date_end = _resolve_date_range(job.date_start, job.date_end, source_db, source_table, date_column)
    lookback_days = max(value_plan.max_window * 4 + 20, 90)
    source_start = date_start - timedelta(days=lookback_days)
    _ensure_source_columns(source_db, source_table, [code_column, date_column, *value_plan.fields])
    _ensure_source_has_rows(source, date_column, source_start, date_end)
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
        available_at,
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
        rank_value,
        percentile,
        score,
        {{job_id:String}},
        toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR,
        now()
    FROM (
        {postprocessed_sql}
    )
    WHERE trade_date >= {{date_start:Date}}
      AND trade_date <= {{date_end:Date}}
      AND raw_value IS NOT NULL
    """
    return ComputePlan(sql=sql, params=base_params, date_start=date_start, date_end=date_end, params_hash=params_hash)


def _postprocess_config(factor: FactorOut) -> PostprocessConfig:
    raw = factor.params.get("data_processing")
    configured = raw if isinstance(raw, dict) else {}
    is_boolean = factor.output_type == "boolean"
    winsorize = "none" if is_boolean else str(configured.get("winsorize") or "quantile").strip().lower()
    standardize = "none" if is_boolean else str(configured.get("standardize") or "zscore").strip().lower()
    neutralize_raw = configured.get("neutralize") or []
    if not isinstance(neutralize_raw, list):
        raise ValueError("data_processing.neutralize 必须是数组")
    neutralize = tuple(str(item).strip().lower() for item in neutralize_raw if str(item).strip())

    allowed_winsorize = {"none", "median", "mad", "quantile"}
    allowed_standardize = {"none", "zscore", "rank"}
    if winsorize not in allowed_winsorize:
        raise ValueError(f"不支持的去极值方式: {winsorize}")
    if standardize not in allowed_standardize:
        raise ValueError(f"不支持的标准化方式: {standardize}")
    if neutralize:
        raise ValueError(
            "当前因子源尚未绑定行业和市值暴露，不能执行中性化: "
            + ", ".join(neutralize)
        )
    return PostprocessConfig(
        winsorize=winsorize,
        standardize=standardize,
        neutralize=neutralize,
    )


def _formula_params(factor: FactorOut, overrides: dict) -> dict:
    """Return only declared formula parameters, excluding UI/processing metadata."""
    declared = set(factor.param_schema)
    if not declared:
        declared = {
            str(name)
            for name, value in factor.params.items()
            if not str(name).startswith("_")
            and str(name) not in {"data_processing", "weighting"}
            and isinstance(value, (bool, int, float, str))
        }
    unknown = sorted(set(overrides) - declared)
    if unknown:
        raise ValueError("任务包含未声明参数: " + ", ".join(unknown))
    return {
        name: overrides[name] if name in overrides else factor.params[name]
        for name in sorted(declared)
        if name in overrides or name in factor.params
    }


def _build_postprocessed_sql(
    raw_sql: str,
    *,
    output_type: str,
    processing: PostprocessConfig,
) -> str:
    """Add daily cross-sectional rank, percentile and model-ready score."""
    raw_cte = f"""
    raw_values AS (
        SELECT trade_date, entity_code, toFloat64(raw_value) AS raw_value
        FROM ({raw_sql})
        WHERE raw_value IS NOT NULL AND isFinite(raw_value)
    )
    """

    if output_type == "boolean":
        winsor_ctes = """
        processed_values AS (
            SELECT trade_date, entity_code, raw_value, raw_value AS processed_value
            FROM raw_values
        )
        """
    elif processing.winsorize == "quantile":
        winsor_ctes = """
        bounds AS (
            SELECT
                trade_date,
                entity_code,
                raw_value,
                quantile(0.01)(raw_value) OVER (PARTITION BY trade_date) AS lower_bound,
                quantile(0.99)(raw_value) OVER (PARTITION BY trade_date) AS upper_bound
            FROM raw_values
        ),
        processed_values AS (
            SELECT
                trade_date,
                entity_code,
                raw_value,
                least(greatest(raw_value, lower_bound), upper_bound) AS processed_value
            FROM bounds
        )
        """
    elif processing.winsorize in {"median", "mad"}:
        multiple = 5.0 if processing.winsorize == "median" else 3.0
        winsor_ctes = f"""
        centers AS (
            SELECT
                trade_date,
                entity_code,
                raw_value,
                median(raw_value) OVER (PARTITION BY trade_date) AS center
            FROM raw_values
        ),
        dispersions AS (
            SELECT
                trade_date,
                entity_code,
                raw_value,
                center,
                median(abs(raw_value - center)) OVER (PARTITION BY trade_date) * 1.4826 AS robust_sigma
            FROM centers
        ),
        processed_values AS (
            SELECT
                trade_date,
                entity_code,
                raw_value,
                if(
                    robust_sigma = 0,
                    raw_value,
                    least(
                        greatest(raw_value, center - {multiple} * robust_sigma),
                        center + {multiple} * robust_sigma
                    )
                ) AS processed_value
            FROM dispersions
        )
        """
    else:
        winsor_ctes = """
        processed_values AS (
            SELECT trade_date, entity_code, raw_value, raw_value AS processed_value
            FROM raw_values
        )
        """

    if output_type == "boolean":
        score_sql = "raw_value"
    elif processing.standardize == "rank":
        score_sql = "percentile"
    elif processing.standardize == "none":
        score_sql = "processed_value"
    else:
        score_sql = "if(processed_stddev = 0, 0.0, (processed_value - processed_mean) / processed_stddev)"

    return f"""
    WITH
    {raw_cte},
    {winsor_ctes},
    ranked_values AS (
        SELECT
            trade_date,
            entity_code,
            raw_value,
            processed_value,
            toUInt32(rank() OVER (PARTITION BY trade_date ORDER BY raw_value DESC)) AS rank_value,
            toFloat64(percent_rank() OVER (PARTITION BY trade_date ORDER BY raw_value ASC)) AS percentile
        FROM processed_values
    ),
    scored_values AS (
        SELECT
            *,
            avg(processed_value) OVER (PARTITION BY trade_date) AS processed_mean,
            stddevPop(processed_value) OVER (PARTITION BY trade_date) AS processed_stddev
        FROM ranked_values
    )
    SELECT
        trade_date,
        entity_code,
        raw_value,
        rank_value,
        percentile,
        toFloat64({score_sql}) AS score
    FROM scored_values
    """


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
    """, max_window=compiled.max_window, fields=compiled.fields)


def _resolve_date_range(
    date_start: Optional[date],
    date_end: Optional[date],
    source_db: str,
    source_table: str,
    date_column: str,
) -> tuple[date, date]:
    if date_end is None:
        rows = client().query(f"SELECT count(), max({date_column}) FROM {source_db}.{source_table}").result_rows
        date_end = rows[0][1] if rows and int(rows[0][0] or 0) > 0 else None
    if date_start is None:
        date_start = date_end
    if date_start is None or date_end is None:
        raise ValueError("缺少计算日期范围，且源表没有可用日期")
    if date_start > date_end:
        raise ValueError("开始日期不能晚于结束日期")
    return date_start, date_end


def _ensure_source_columns(source_db: str, source_table: str, fields: list[str]) -> None:
    rows = client().query(f"DESCRIBE TABLE {source_db}.{source_table}").result_rows
    columns = {row[0] for row in rows}
    missing = sorted({field for field in fields if field not in columns})
    if missing:
        raise ValueError(f"源表缺少因子所需字段: {', '.join(missing)}")


def _ensure_source_has_rows(source: str, date_column: str, source_start: date, date_end: date) -> None:
    rows = client().query(
        f"""
        SELECT count()
        FROM {source}
        WHERE {date_column} >= {{source_start:Date}}
          AND {date_column} <= {{date_end:Date}}
        """,
        parameters={"source_start": source_start, "date_end": date_end},
    ).result_rows
    if not rows or int(rows[0][0] or 0) == 0:
        raise ValueError("源表在计算日期范围内没有可用数据")


def _count_job_values(job_id: str) -> int:
    database = _identifier(settings().clickhouse_database, "factor database")
    rows = client().query(
        f"SELECT count() FROM {database}.factor_values_daily WHERE job_id = {{job_id:String}}",
        parameters={"job_id": job_id},
    ).result_rows
    return int(rows[0][0] or 0)


def _cleanup_superseded_values(plan: ComputePlan) -> None:
    """Keep the newly written batch and remove older rows for the same keys/range."""
    database = _identifier(settings().clickhouse_database, "factor database")
    client().command(
        f"""
        ALTER TABLE {database}.factor_values_daily DELETE
        WHERE factor_id = {{factor_id:String}}
          AND factor_version = {{factor_version:UInt32}}
          AND entity_type = {{entity_type:String}}
          AND params_hash = {{params_hash:String}}
          AND trade_date >= {{date_start:Date}}
          AND trade_date <= {{date_end:Date}}
          AND job_id != {{job_id:String}}
        SETTINGS mutations_sync = 2
        """,
        parameters={
            "factor_id": plan.params["factor_id"],
            "factor_version": plan.params["factor_version"],
            "entity_type": plan.params["entity_type"],
            "params_hash": plan.params["params_hash"],
            "date_start": plan.date_start,
            "date_end": plan.date_end,
            "job_id": plan.params["job_id"],
        },
    )


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
