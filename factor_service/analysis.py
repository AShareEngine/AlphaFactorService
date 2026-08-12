from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any, Optional

import pandas as pd

from factor_service import repository
from factor_service.clickhouse import client, settings
from factor_service.schemas import FactorAnalysisJobOut


logger = logging.getLogger(__name__)
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class AnalysisPayload:
    summary_rows: list[tuple]
    ic_rows: list[tuple]
    quantile_return_rows: list[tuple]
    turnover_rows: list[tuple]
    row_count: int


def run_pending_analysis_jobs(limit: int = 3) -> list[FactorAnalysisJobOut]:
    jobs = repository.list_analysis_jobs(status="pending", limit=limit)
    return [run_analysis_job(job.analysis_job_id) for job in jobs]


def run_analysis_job(analysis_job_id: str) -> FactorAnalysisJobOut:
    job = repository.get_analysis_job(analysis_job_id)
    if not job:
        raise ValueError(f"分析任务不存在: {analysis_job_id}")
    if job.status == "success":
        return job
    if job.status == "running":
        return job

    started_at = datetime.now()
    repository.update_analysis_job_status(job.analysis_job_id, "running", started_at=started_at)
    try:
        payload = build_analysis_payload(job)
        repository.replace_analysis_results(
            job.analysis_job_id,
            summary_rows=payload.summary_rows,
            ic_rows=payload.ic_rows,
            quantile_return_rows=payload.quantile_return_rows,
            turnover_rows=payload.turnover_rows,
        )
        return repository.update_analysis_job_status(
            job.analysis_job_id,
            "success",
            row_count=payload.row_count,
            started_at=started_at,
            finished_at=datetime.now(),
        )
    except Exception as exc:
        logger.exception("factor analysis job failed: %s", job.analysis_job_id)
        return repository.update_analysis_job_status(
            job.analysis_job_id,
            "failed",
            error_message=str(exc),
            started_at=started_at,
            finished_at=datetime.now(),
        )


def build_analysis_payload(job: FactorAnalysisJobOut) -> AnalysisPayload:
    try:
        from alphalens import performance, utils
    except ImportError as exc:
        raise RuntimeError("因子分析需要安装 alphalens-reloaded") from exc

    periods = sorted({int(item) for item in job.periods if int(item) > 0})
    if not periods:
        raise ValueError("分析周期不能为空")

    date_start, date_end = _resolve_analysis_range(job)
    factor = _load_factor_series(job, date_start, date_end)
    price_end = date_end + timedelta(days=max(periods) * 4 + 15)
    prices = _load_prices(job, date_start, price_end)
    factor = _shift_factor_to_next_trading_day(factor, prices.index)

    factor_data = utils.get_clean_factor_and_forward_returns(
        factor=factor,
        prices=prices,
        quantiles=job.quantiles,
        periods=tuple(periods),
        filter_zscore=None,
        max_loss=job.max_loss,
        cumulative_returns=job.cumulative_returns,
    )
    forward_columns = list(utils.get_forward_returns_columns(factor_data.columns))
    if not forward_columns:
        raise ValueError("Alphalens 没有生成可用的 forward return")

    ic = performance.factor_information_coefficient(factor_data)
    mean_return_by_quantile, _ = performance.mean_return_by_quantile(
        factor_data,
        by_date=True,
        demeaned=False,
    )

    summary_rows = _build_summary_rows(job, factor_data, ic, mean_return_by_quantile, forward_columns)
    ic_rows = _build_ic_rows(job, ic)
    quantile_return_rows = _build_quantile_return_rows(job, mean_return_by_quantile)
    turnover_rows, turnover_summary_rows = _build_turnover_rows(job, performance, factor_data, periods)
    summary_rows.extend(turnover_summary_rows)

    return AnalysisPayload(
        summary_rows=summary_rows,
        ic_rows=ic_rows,
        quantile_return_rows=quantile_return_rows,
        turnover_rows=turnover_rows,
        row_count=int(len(factor_data)),
    )


def _resolve_analysis_range(job: FactorAnalysisJobOut) -> tuple[date, date]:
    database = _identifier(settings().clickhouse_database, "factor database")
    conditions = [
        "factor_id = {factor_id:String}",
        "entity_type = {entity_type:String}",
        "factor_version = {factor_version:UInt32}",
    ]
    params: dict[str, Any] = {
        "factor_id": job.factor_id,
        "entity_type": job.entity_type,
        "factor_version": job.factor_version,
    }
    if job.params_hash:
        conditions.append("params_hash = {params_hash:String}")
        params["params_hash"] = job.params_hash
    rows = client().query(
        f"""
        SELECT min(trade_date), max(trade_date)
        FROM {database}.factor_values_daily
        WHERE {' AND '.join(conditions)}
        """,
        parameters=params,
    ).result_rows
    available_start, available_end = rows[0] if rows else (None, None)
    date_start = job.date_start or available_start
    date_end = job.date_end or available_end
    if date_start is None or date_end is None:
        raise ValueError("缺少分析日期范围，且没有可用因子结果")
    if date_start > date_end:
        raise ValueError("开始日期不能晚于结束日期")
    return date_start, date_end


def _load_factor_series(job: FactorAnalysisJobOut, date_start: date, date_end: date) -> pd.Series:
    database = _identifier(settings().clickhouse_database, "factor database")
    conditions = [
        "factor_id = {factor_id:String}",
        "entity_type = {entity_type:String}",
        "factor_version = {factor_version:UInt32}",
        "trade_date >= {date_start:Date}",
        "trade_date <= {date_end:Date}",
        "raw_value IS NOT NULL",
    ]
    params: dict[str, Any] = {
        "factor_id": job.factor_id,
        "entity_type": job.entity_type,
        "factor_version": job.factor_version,
        "date_start": date_start,
        "date_end": date_end,
    }
    if job.params_hash:
        conditions.append("params_hash = {params_hash:String}")
        params["params_hash"] = job.params_hash
    rows = client().query(
        f"""
        SELECT trade_date, entity_code, raw_value
        FROM (
            SELECT
                trade_date,
                entity_code,
                raw_value,
                row_number() OVER (
                    PARTITION BY trade_date, entity_code
                    ORDER BY updated_at DESC
                ) AS rn
            FROM {database}.factor_values_daily
            WHERE {' AND '.join(conditions)}
        )
        WHERE rn = 1
        ORDER BY trade_date ASC, entity_code ASC
        """,
        parameters=params,
    ).result_rows
    if not rows:
        raise ValueError("没有可用于分析的因子值")
    frame = pd.DataFrame(rows, columns=["date", "asset", "factor"])
    frame["date"] = pd.to_datetime(frame["date"])
    frame["asset"] = frame["asset"].astype(str)
    frame["factor"] = pd.to_numeric(frame["factor"], errors="coerce")
    frame = frame.dropna(subset=["factor"])
    if frame.empty:
        raise ValueError("因子值全部为空，无法分析")
    index = pd.MultiIndex.from_frame(frame[["date", "asset"]], names=["date", "asset"])
    return pd.Series(frame["factor"].to_numpy(dtype=float), index=index, name="factor").sort_index()


def _load_prices(job: FactorAnalysisJobOut, date_start: date, date_end: date) -> pd.DataFrame:
    if job.entity_type != "stock":
        raise ValueError(f"暂不支持的分析实体类型: {job.entity_type}")
    config = settings()
    source_db = _identifier(config.source_database, "source database")
    source_table = _identifier(config.stock_daily_table, "stock daily table")
    code_column = _identifier(config.stock_code_column, "stock code column")
    date_column = _identifier(config.stock_date_column, "stock date column")
    price_column = _identifier(job.price_field or config.stock_price_column, "price field")
    stock_basic_table = _identifier(config.stock_basic_table, "stock basic table")
    stock_type_column = _identifier(config.stock_basic_type_column, "stock basic type column")
    rows = client().query(
        f"""
        SELECT {date_column} AS date, {code_column} AS asset, {price_column} AS price
        FROM {source_db}.{source_table}
        WHERE {date_column} >= {{date_start:Date}}
          AND {date_column} <= {{date_end:Date}}
          AND {price_column} IS NOT NULL
          AND {code_column} IN (
              SELECT {code_column}
              FROM {source_db}.{stock_basic_table}
              WHERE {stock_type_column} = {{stock_type_value:String}}
          )
        ORDER BY date ASC, asset ASC
        """,
        parameters={
            "date_start": date_start,
            "date_end": date_end,
            "stock_type_value": config.stock_basic_stock_type_value,
        },
    ).result_rows
    if not rows:
        raise ValueError("没有可用于分析的价格数据")
    frame = pd.DataFrame(rows, columns=["date", "asset", "price"])
    frame["date"] = pd.to_datetime(frame["date"])
    frame["asset"] = frame["asset"].astype(str)
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    prices = frame.pivot_table(index="date", columns="asset", values="price", aggfunc="last")
    prices = prices.sort_index()
    if prices.empty:
        raise ValueError("价格矩阵为空，无法分析")
    return prices


def _shift_factor_to_next_trading_day(
    factor: pd.Series,
    price_dates: pd.Index,
) -> pd.Series:
    """Make close-derived signals tradable before Alphalens measures returns.

    A daily factor can use that day's close, so it is only known after the
    close. Alphalens otherwise starts its forward return at the same day's
    close. Relabeling each observation to the next market date makes the
    evaluation use the next close and prevents same-close execution leakage.
    """
    calendar = pd.DatetimeIndex(price_dates).drop_duplicates().sort_values()
    if calendar.empty:
        raise ValueError("价格交易日历为空，无法延迟因子信号")

    frame = factor.rename("factor").reset_index()
    frame["date"] = pd.to_datetime(frame["date"])
    positions = calendar.searchsorted(frame["date"], side="right")
    valid = positions < len(calendar)
    frame = frame.loc[valid].copy()
    if frame.empty:
        raise ValueError("因子区间后没有下一交易日价格，无法分析")
    frame["date"] = calendar.take(positions[valid])
    frame = frame.drop_duplicates(["date", "asset"], keep="last")
    index = pd.MultiIndex.from_frame(
        frame[["date", "asset"]],
        names=["date", "asset"],
    )
    return pd.Series(
        frame["factor"].to_numpy(dtype=float),
        index=index,
        name="factor",
    ).sort_index()


def _build_summary_rows(
    job: FactorAnalysisJobOut,
    factor_data: pd.DataFrame,
    ic: pd.DataFrame,
    mean_return_by_quantile: pd.DataFrame,
    forward_columns: list[str],
) -> list[tuple]:
    rows: list[tuple] = [
        _summary(job, "clean_factor_rows", "", float(len(factor_data))),
        _summary(job, "factor_date_count", "", float(factor_data.index.get_level_values("date").nunique())),
        _summary(job, "asset_count", "", float(factor_data.index.get_level_values("asset").nunique())),
        _summary(job, "quantiles", "", float(job.quantiles)),
        _summary(job, "signal_lag_trading_days", "", 1.0),
    ]
    for period in forward_columns:
        series = pd.to_numeric(ic[period], errors="coerce").dropna() if period in ic else pd.Series(dtype=float)
        rows.extend(
            [
                _summary(job, "ic_mean", period, _safe_float(series.mean())),
                _summary(job, "ic_std", period, _safe_float(series.std())),
                _summary(job, "ic_positive_ratio", period, _safe_float((series > 0).mean())),
                _summary(job, "ic_observation_count", period, float(len(series))),
            ]
        )
        if period in mean_return_by_quantile:
            by_quantile = mean_return_by_quantile[period].groupby(level="factor_quantile").mean()
            if not by_quantile.empty:
                bottom_quantile = int(by_quantile.index.min())
                top_quantile = int(by_quantile.index.max())
                top_value = _safe_float(by_quantile.loc[top_quantile])
                bottom_value = _safe_float(by_quantile.loc[bottom_quantile])
                spread_value = top_value - bottom_value if top_value is not None and bottom_value is not None else None
                rows.extend(
                    [
                        _summary(job, "top_quantile_return_mean", period, top_value),
                        _summary(job, "bottom_quantile_return_mean", period, bottom_value),
                        _summary(job, "quantile_spread_mean", period, _safe_float(spread_value)),
                    ]
                )
    return rows


def _build_ic_rows(job: FactorAnalysisJobOut, ic: pd.DataFrame) -> list[tuple]:
    rows: list[tuple] = []
    for date_index, values in ic.iterrows():
        trade_date = _as_date(date_index)
        for period, value in values.items():
            rows.append((job.analysis_job_id, trade_date, str(period), _safe_float(value)))
    return rows


def _build_quantile_return_rows(
    job: FactorAnalysisJobOut,
    mean_return_by_quantile: pd.DataFrame,
) -> list[tuple]:
    rows: list[tuple] = []
    for index, values in mean_return_by_quantile.iterrows():
        if not isinstance(index, tuple) or len(index) < 2:
            continue
        quantile = int(index[0])
        trade_date = _as_date(index[1])
        for period, value in values.items():
            rows.append((job.analysis_job_id, trade_date, str(period), quantile, _safe_float(value)))
    return rows


def _build_turnover_rows(
    job: FactorAnalysisJobOut,
    performance,
    factor_data: pd.DataFrame,
    periods: list[int],
) -> tuple[list[tuple], list[tuple]]:
    rows: list[tuple] = []
    summary_rows: list[tuple] = []
    quantile_factor = factor_data["factor_quantile"]
    for period in periods:
        period_label = f"{period}D"
        rank_autocorrelation = performance.factor_rank_autocorrelation(factor_data, period=period)
        rank_by_date = {
            _as_date(index): _safe_float(value)
            for index, value in rank_autocorrelation.dropna().items()
        }
        summary_rows.append(
            _summary(job, "rank_autocorrelation_mean", period_label, _safe_float(rank_autocorrelation.mean()))
        )
        for quantile in range(1, job.quantiles + 1):
            turnover = performance.quantile_turnover(quantile_factor, quantile, period=period)
            clean_turnover = turnover.dropna()
            summary_rows.append(
                _summary(job, "quantile_turnover_mean", f"{period_label}:Q{quantile}", _safe_float(clean_turnover.mean()))
            )
            for date_index, value in clean_turnover.items():
                trade_date = _as_date(date_index)
                rows.append(
                    (
                        job.analysis_job_id,
                        trade_date,
                        period_label,
                        quantile,
                        _safe_float(value),
                        rank_by_date.get(trade_date),
                    )
                )
    return rows, summary_rows


def _summary(job: FactorAnalysisJobOut, metric: str, period: str, value: Optional[float]) -> tuple:
    payload = {"factor_id": job.factor_id, "factor_version": job.factor_version}
    return (
        job.analysis_job_id,
        metric,
        period,
        value,
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.match(value or ""):
        raise ValueError(f"{label} 不是合法标识: {value}")
    return value


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _as_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return pd.Timestamp(value).date()
