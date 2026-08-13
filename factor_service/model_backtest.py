from __future__ import annotations

from datetime import date, datetime
import json
from math import isfinite, sqrt
from typing import Any, Optional

import pandas as pd

from factor_service.clickhouse import client, settings
from factor_service.factor_backtest import (
    UNIVERSES,
    _annualized_return,
    _apply_universe_and_sample_filters,
    _load_benchmark,
    _load_market,
    _max_drawdown,
    _safe_float,
    _sample_filters,
    _simulate_quantile_portfolio,
)
from factor_service import model_repository
from factor_service.schemas import ModelBacktestJobOut


def run_model_backtest_job(backtest_job_id: str) -> ModelBacktestJobOut:
    job = model_repository.get_model_backtest_job(backtest_job_id)
    if job is None:
        raise ValueError("模型回测任务不存在")
    if job.status in {"success", "running"}:
        return job
    model_repository.update_model_backtest_job(
        backtest_job_id, status="running", error_message="", started_at=datetime.now(),
    )
    try:
        date_start, date_end = _resolve_model_range(job)
        job = model_repository.update_model_backtest_job(
            backtest_job_id, date_start=date_start, date_end=date_end,
        )
        signals = _load_model_signals(job)
        if signals.empty:
            raise ValueError("模型版本在所选范围没有预测分数")
        calendar, benchmark_returns = _load_benchmark(job)  # type: ignore[arg-type]
        signals = _apply_universe_and_sample_filters(job, signals, calendar)  # type: ignore[arg-type]
        if signals.empty:
            raise ValueError("股票池和样本过滤后没有模型预测")
        codes = sorted(set(signals["code"].astype(str)))
        market = _load_market(job, codes, calendar)  # type: ignore[arg-type]
        if market.empty:
            raise ValueError("缺少股票开盘价和交易状态")
        targets, sample_counts = _build_top_n_targets(
            signals, market, calendar,
            top_n=job.top_n,
            rebalance_every=job.rebalance_every,
            configuration=job.configuration,
        )
        portfolio = _simulate_quantile_portfolio(
            targets, market, calendar, job.buy_cost_rate, job.sell_cost_rate,
        )
        daily = _combine_model_daily(job, portfolio, benchmark_returns, sample_counts)
        if daily.empty:
            raise ValueError("没有形成可计算的模型组合收益")
        portfolio_returns = daily["portfolio_return"].fillna(0.0)
        excess_returns = daily["excess_return"].fillna(0.0)
        std = _safe_float(portfolio_returns.std(ddof=1))
        sharpe = None
        if std not in (None, 0.0):
            sharpe = _safe_float(sqrt(252.0) * portfolio_returns.mean() / std)
        payload = {
            "model_id": job.model_id,
            "model_version": job.model_version,
            "universe_id": job.universe_id,
            "benchmark_code": job.benchmark_code,
            "top_n": job.top_n,
            "rebalance_every": job.rebalance_every,
            "signal_lag_trading_days": 1,
            "execution_price": "next_open_backward_adjusted",
            "sample_filters": _sample_filters(job.configuration),
        }
        rows = [
            (
                job.backtest_job_id, row.trade_date,
                _safe_float(row.portfolio_return), _safe_float(row.benchmark_return),
                _safe_float(row.excess_return), _safe_float(row.portfolio_nav),
                _safe_float(row.benchmark_nav), _safe_float(row.turnover),
                _safe_float(row.transaction_cost), int(row.sample_count),
                int(row.holding_count), int(row.blocked_buy_count),
                int(row.blocked_sell_count), row.holdings_json,
            )
            for row in daily.itertuples(index=False)
        ]
        model_repository.replace_model_backtest_daily(backtest_job_id, rows)
        return model_repository.update_model_backtest_job(
            backtest_job_id,
            status="success",
            annual_return=_annualized_return(portfolio_returns),
            excess_annual_return=_annualized_return(excess_returns),
            sharpe_ratio=sharpe,
            turnover_rate=_safe_float(daily["turnover"].mean()),
            max_drawdown=_max_drawdown(daily["portfolio_nav"]),
            trading_days=len(daily),
            payload=payload,
            finished_at=datetime.now(),
        )
    except Exception as exc:
        return model_repository.update_model_backtest_job(
            backtest_job_id, status="failed", error_message=str(exc),
            finished_at=datetime.now(),
        )


def _resolve_model_range(job: ModelBacktestJobOut) -> tuple[date, date]:
    database = settings().model_database
    prediction_range = client().query(
        f"""
        SELECT min(trade_date), max(trade_date)
        FROM {database}.model_predictions_daily
        WHERE model_id = {{model_id:String}} AND model_version = {{version:UInt32}}
        """,
        parameters={"model_id": job.model_id, "version": job.model_version},
    ).result_rows[0]
    market_latest = client().query(
        "SELECT max(toDate(trade_time)) FROM starlight.ad_market_kline_daily WHERE code = {code:String}",
        parameters={"code": job.benchmark_code},
    ).result_rows[0][0]
    if prediction_range[0] is None or prediction_range[1] is None or market_latest is None:
        raise ValueError("模型预测或基准行情缺少可用范围")
    end = min(prediction_range[1], market_latest)
    if job.date_preset == "custom":
        start = max(job.requested_date_start, prediction_range[0])  # type: ignore[arg-type]
        end = min(end, job.requested_date_end)  # type: ignore[arg-type]
    else:
        offsets = {
            "3m": pd.DateOffset(months=3), "1y": pd.DateOffset(years=1),
            "3y": pd.DateOffset(years=3), "10y": pd.DateOffset(years=10),
        }
        start = max((pd.Timestamp(end) - offsets[job.date_preset]).date(), prediction_range[0])
    if start >= end:
        raise ValueError("模型预测覆盖不足以形成回测区间")
    return start, end


def _load_model_signals(job: ModelBacktestJobOut) -> pd.DataFrame:
    rows = client().query(
        f"""
        SELECT trade_date, entity_code, score
        FROM {settings().model_database}.model_predictions_daily FINAL
        WHERE model_id = {{model_id:String}}
          AND model_version = {{version:UInt32}}
          AND trade_date >= {{date_start:Date}} - INTERVAL 7 DAY
          AND trade_date <= {{date_end:Date}}
          AND toDate(feature_cutoff_at) <= trade_date
        ORDER BY trade_date, entity_code
        """,
        parameters={
            "model_id": job.model_id, "version": job.model_version,
            "date_start": job.date_start, "date_end": job.date_end,
        },
    ).result_rows
    frame = pd.DataFrame(rows, columns=["signal_date", "code", "score"])
    if frame.empty:
        return frame
    frame["signal_date"] = pd.to_datetime(frame["signal_date"])
    frame["score"] = pd.to_numeric(frame["score"], errors="coerce")
    return frame.dropna(subset=["signal_date", "score"])


def _build_top_n_targets(
    signals: pd.DataFrame,
    market: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    *,
    top_n: int,
    rebalance_every: int,
    configuration: Optional[dict[str, Any]] = None,
) -> tuple[dict[pd.Timestamp, dict[str, float]], dict[pd.Timestamp, int]]:
    filters = _sample_filters(configuration)
    state_lookup = market.set_index(["date", "code"])
    targets: dict[pd.Timestamp, dict[str, float]] = {}
    counts: dict[pd.Timestamp, int] = {}
    selected_signal_index = 0
    for signal_date, group in signals.groupby("signal_date", sort=True):
        if selected_signal_index % max(1, int(rebalance_every)) != 0:
            selected_signal_index += 1
            continue
        selected_signal_index += 1
        position = calendar.searchsorted(signal_date, side="right")
        if position >= len(calendar):
            continue
        execution_date = calendar[position]
        eligible: list[tuple[str, float]] = []
        for row in group.itertuples(index=False):
            key = (execution_date, row.code)
            if key not in state_lookup.index:
                continue
            state = state_lookup.loc[key]
            if filters["exclude_limit_paused"] and (
                not bool(state["buy_allowed"]) or not bool(state["sell_allowed"])
            ):
                continue
            if filters["exclude_st"] and int(state["is_st"]) == 1:
                continue
            if filters["exclude_delisting"] and int(state["is_withdrawal"]) == 1:
                continue
            value = float(row.score)
            if isfinite(value):
                eligible.append((str(row.code), value))
        if len(eligible) < int(top_n):
            continue
        selected = sorted(eligible, key=lambda item: (-item[1], item[0]))[: int(top_n)]
        targets[execution_date] = {code: 1.0 / len(selected) for code, _ in selected}
        counts[execution_date] = len(eligible)
    return targets, counts


def _combine_model_daily(
    job: ModelBacktestJobOut,
    portfolio: dict[pd.Timestamp, Any],
    benchmark_returns: pd.Series,
    sample_counts: dict[pd.Timestamp, int],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    portfolio_nav = benchmark_nav = 1.0
    latest_sample_count = 0
    for trade_date in sorted(portfolio):
        if trade_date < pd.Timestamp(job.date_start) or trade_date > pd.Timestamp(job.date_end):
            continue
        day = portfolio[trade_date]
        benchmark = _safe_float(benchmark_returns.get(trade_date))
        if benchmark is None:
            continue
        latest_sample_count = sample_counts.get(trade_date, latest_sample_count)
        portfolio_nav *= 1.0 + day.net_return
        benchmark_nav *= 1.0 + benchmark
        rows.append({
            "trade_date": trade_date.date(),
            "portfolio_return": day.net_return,
            "benchmark_return": benchmark,
            "excess_return": day.net_return - benchmark,
            "portfolio_nav": portfolio_nav,
            "benchmark_nav": benchmark_nav,
            "turnover": day.turnover,
            "transaction_cost": day.cost,
            "sample_count": latest_sample_count,
            "holding_count": len(day.holdings),
            "blocked_buy_count": day.blocked_buy_count,
            "blocked_sell_count": day.blocked_sell_count,
            "holdings_json": json.dumps(list(day.holdings), ensure_ascii=False),
        })
    return pd.DataFrame(rows)


__all__ = ["_build_top_n_targets", "run_model_backtest_job"]
