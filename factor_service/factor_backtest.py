from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any, Iterable, Optional

import pandas as pd

from factor_service import repository
from factor_service.clickhouse import client, settings
from factor_service.schemas import FactorBacktestJobOut


logger = logging.getLogger(__name__)

UNIVERSES = {
    "csi300": {"index_code": "000300.SH", "benchmark": "000300.SH"},
    "csi500": {"index_code": "000905.SH", "benchmark": "000905.SH"},
    "csi800": {"index_code": "000906.SH", "benchmark": "000906.SH"},
    "csi1000": {"index_code": "000852.SH", "benchmark": "000852.SH"},
    "all_a": {"index_code": "000985.SH", "benchmark": "000985.SH"},
}

DEFAULT_SAMPLE_FILTERS = {
    "exclude_limit_paused": True,
    "exclude_st": False,
    "exclude_new_stocks": False,
    "exclude_delisting": False,
    "exclude_bse": False,
    "minimum_listing_trading_days": 60,
}


@dataclass(frozen=True)
class PortfolioDay:
    trade_date: date
    net_return: float
    turnover: float
    cost: float
    blocked_buy_count: int
    blocked_sell_count: int
    holdings: tuple[str, ...] = ()


@dataclass(frozen=True)
class BacktestFactorResult:
    summary_row: tuple
    daily_rows: list[tuple]


def run_factor_backtest_job(backtest_job_id: str) -> FactorBacktestJobOut:
    job = repository.get_factor_backtest_job(backtest_job_id)
    if job is None:
        raise ValueError("因子回测任务不存在")
    if job.status == "success":
        return job
    if job.status == "running":
        return job
    started_at = datetime.now()
    repository.update_factor_backtest_job(
        job.backtest_job_id, status="running", error_message="", started_at=started_at,
    )
    try:
        date_start, date_end = resolve_backtest_range(job)
        job = repository.update_factor_backtest_job(
            job.backtest_job_id, date_start=date_start, date_end=date_end,
        )
        completed = 0
        failed = 0
        for factor_id in job.factor_ids:
            try:
                result = build_factor_backtest_result(job, factor_id)
            except Exception as exc:
                failed += 1
                logger.exception("factor backtest failed: %s %s", job.backtest_job_id, factor_id)
                try:
                    failed_version, failed_params_hash = _resolve_signal_revision(job, factor_id)
                except Exception:
                    factor = repository.get_factor(factor_id)
                    failed_version = factor.version if factor else 0
                    failed_params_hash = ""
                result = BacktestFactorResult(
                    summary_row=(
                        job.backtest_job_id, factor_id, failed_version, failed_params_hash,
                        "failed", str(exc), None, None, None, None, None, None,
                        None, 0, 0, json.dumps({}, ensure_ascii=False),
                    ),
                    daily_rows=[],
                )
            repository.replace_factor_backtest_factor_results(
                job.backtest_job_id, factor_id,
                summary_row=result.summary_row, daily_rows=result.daily_rows,
            )
            completed += 1
            repository.update_factor_backtest_job(
                job.backtest_job_id, completed_factors=completed,
            )
        message = f"{failed} 个因子回测失败" if failed else ""
        return repository.update_factor_backtest_job(
            job.backtest_job_id,
            status="failed" if failed == completed else "success",
            error_message=message,
            completed_factors=completed, finished_at=datetime.now(),
        )
    except Exception as exc:
        logger.exception("factor backtest job failed: %s", job.backtest_job_id)
        return repository.update_factor_backtest_job(
            job.backtest_job_id, status="failed", error_message=str(exc),
            finished_at=datetime.now(),
        )


def resolve_backtest_range(job: FactorBacktestJobOut) -> tuple[date, date]:
    if job.date_preset == "custom":
        if job.requested_date_start is None or job.requested_date_end is None:
            raise ValueError("自定义回测缺少日期范围")
        requested_start = job.requested_date_start
        requested_end = job.requested_date_end
        end = min(requested_end, _common_latest_date(job))
    else:
        requested_end = _common_latest_date(job)
        offsets = {"3m": pd.DateOffset(months=3), "1y": pd.DateOffset(years=1),
                   "3y": pd.DateOffset(years=3), "10y": pd.DateOffset(years=10)}
        requested_start = (pd.Timestamp(requested_end) - offsets[job.date_preset]).date()
        end = requested_end
    if requested_start >= end:
        raise ValueError("所选时间范围没有足够的共同数据")
    return requested_start, end


def _common_latest_date(job: FactorBacktestJobOut) -> date:
    database = settings().clickhouse_database
    factor_latest = client().query(
        f"""
        SELECT min(latest_date)
        FROM (
            SELECT factor_id, max(trade_date) AS latest_date
            FROM {database}.factor_values_daily
            WHERE factor_id IN {{factor_ids:Array(String)}} AND score IS NOT NULL
            GROUP BY factor_id
        )
        """,
        parameters={"factor_ids": job.factor_ids},
    ).result_rows[0][0]
    market_latest = client().query(
        "SELECT max(toDate(trade_time)) FROM starlight.ad_market_kline_daily "
        "WHERE code = {code:String}",
        parameters={"code": job.benchmark_code},
    ).result_rows[0][0]
    if factor_latest is None or market_latest is None:
        raise ValueError("因子或基准缺少可用数据")
    return min(factor_latest, market_latest)


def build_factor_backtest_result(
    job: FactorBacktestJobOut, factor_id: str,
) -> BacktestFactorResult:
    version, params_hash = _resolve_signal_revision(job, factor_id)
    factor = repository.get_factor(factor_id, version=version)
    if factor is None:
        raise ValueError(f"因子定义版本 {version} 不存在")
    signals = _load_signals(job, factor_id, version, params_hash)
    if signals.empty:
        raise ValueError("所选范围没有已同步 score")
    calendar, benchmark_returns = _load_benchmark(job)
    signals = _apply_universe_and_sample_filters(job, signals, calendar)
    if signals.empty:
        raise ValueError("股票池和样本过滤后没有有效因子值")
    codes = sorted(set(signals["code"].astype(str)))
    market = _load_market(job, codes, calendar)
    if market.empty:
        raise ValueError("缺少股票开盘价和交易状态")
    targets_q1, targets_q5, ic_by_date, sample_counts = _build_targets(
        signals, market, calendar, job.configuration,
    )
    q1 = _simulate_quantile_portfolio(
        targets_q1, market, calendar, job.buy_cost_rate, job.sell_cost_rate,
    )
    q5 = _simulate_quantile_portfolio(
        targets_q5, market, calendar, job.buy_cost_rate, job.sell_cost_rate,
    )
    daily = _combine_daily_results(
        job, factor_id, q1, q5, benchmark_returns, ic_by_date, sample_counts,
    )
    if daily.empty:
        raise ValueError("没有形成可计算的持有期收益")
    q5_returns = daily["q5_return"].fillna(0.0)
    excess_returns = daily["excess_return"].fillna(0.0)
    ls_returns = daily["long_short_return"].fillna(0.0)
    ic = daily["ic"].dropna()
    payload = {
        "universe_id": job.universe_id,
        "benchmark_code": job.benchmark_code,
        "signal_field": "score",
        "factor_direction": "q5_minus_q1",
        "signal_lag_trading_days": 1,
        "execution_price": job.execution_price,
        "source_vintage": "factor_values_as_of_job_creation",
        "sample_filters": _sample_filters(job.configuration),
    }
    summary = (
        job.backtest_job_id, factor_id, factor.version, params_hash, "success", "",
        _annualized_return(q5_returns), _annualized_return(excess_returns),
        _annualized_return(ls_returns), _safe_float(daily["turnover"].mean()),
        _safe_float(ic.mean()), _safe_ratio(ic.mean(), ic.std(ddof=1)),
        _max_drawdown(daily["q5_nav"]), int(len(daily)),
        int((daily["sample_count"] >= 25).sum()),
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )
    rows = [
        (
            job.backtest_job_id, factor_id, row.trade_date,
            _safe_float(row.q1_return), _safe_float(row.q5_return),
            _safe_float(row.long_short_return), _safe_float(row.benchmark_return),
            _safe_float(row.excess_return), _safe_float(row.q1_nav),
            _safe_float(row.q5_nav), _safe_float(row.long_short_nav),
            _safe_float(row.benchmark_nav), _safe_float(row.turnover),
            _safe_float(row.transaction_cost), _safe_float(row.ic),
            int(row.sample_count), int(row.blocked_buy_count),
            int(row.blocked_sell_count),
        )
        for row in daily.itertuples(index=False)
    ]
    return BacktestFactorResult(summary_row=summary, daily_rows=rows)


def _resolve_signal_revision(
    job: FactorBacktestJobOut, factor_id: str,
) -> tuple[int, str]:
    database = settings().clickhouse_database
    cutoff = datetime.fromisoformat(str(job.configuration.get("data_cutoff")))
    universe_filter = _signal_universe_filter(job)
    rows = client().query(
        f"""
        SELECT factor_version, params_hash
        FROM {database}.factor_values_daily
        WHERE factor_id = {{factor_id:String}}
          AND greatest(trade_date, toDate(event_available_at)) >= {{date_start:Date}} - INTERVAL 7 DAY
          AND greatest(trade_date, toDate(event_available_at)) <= {{date_end:Date}}
          AND computed_at <= {{cutoff:DateTime}}
          AND score IS NOT NULL
          {universe_filter}
        GROUP BY factor_version, params_hash
        ORDER BY factor_version DESC, max(computed_at) DESC, max(updated_at) DESC
        LIMIT 1
        """,
        parameters={
            "factor_id": factor_id,
            "date_start": job.date_start,
            "date_end": job.date_end,
            "cutoff": cutoff,
        },
    ).result_rows
    if not rows:
        raise ValueError("所选范围没有已同步 score")
    return int(rows[0][0]), str(rows[0][1])


def _load_signals(
    job: FactorBacktestJobOut, factor_id: str, version: int, params_hash: str,
) -> pd.DataFrame:
    database = settings().clickhouse_database
    cutoff = datetime.fromisoformat(str(job.configuration.get("data_cutoff")))
    universe_filter = _signal_universe_filter(job)
    rows = client().query(
        f"""
        SELECT trade_date, entity_code, score, event_available_at
        FROM (
            SELECT trade_date, entity_code, score, event_available_at,
                   row_number() OVER (
                       PARTITION BY trade_date, entity_code
                       ORDER BY computed_at DESC, updated_at DESC
                   ) AS rn
            FROM {database}.factor_values_daily
            WHERE factor_id = {{factor_id:String}}
              AND factor_version = {{version:UInt32}}
              AND params_hash = {{params_hash:String}}
              AND greatest(trade_date, toDate(event_available_at)) >= {{date_start:Date}} - INTERVAL 7 DAY
              AND greatest(trade_date, toDate(event_available_at)) <= {{date_end:Date}}
              AND computed_at <= {{cutoff:DateTime}}
              AND score IS NOT NULL
              {universe_filter}
        ) WHERE rn = 1
        ORDER BY trade_date, entity_code
        """,
        parameters={
            "factor_id": factor_id, "version": version, "params_hash": params_hash,
            "date_start": job.date_start, "date_end": job.date_end, "cutoff": cutoff,
        },
    ).result_rows
    frame = pd.DataFrame(
        rows, columns=["source_trade_date", "code", "score", "event_available_at"],
    )
    return _normalize_signal_dates(frame)


def _normalize_signal_dates(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["signal_date", "code", "score"])
    result = frame.copy()
    result["source_trade_date"] = pd.to_datetime(result["source_trade_date"])
    result["event_available_at"] = pd.to_datetime(result["event_available_at"])
    result["event_date"] = result["event_available_at"].dt.tz_localize(None).dt.normalize()
    result["signal_date"] = result[["source_trade_date", "event_date"]].max(axis=1)
    result["score"] = pd.to_numeric(result["score"], errors="coerce")
    result.dropna(subset=["score", "signal_date"], inplace=True)
    result.sort_values(["signal_date", "source_trade_date", "code"], inplace=True)
    return result[["signal_date", "code", "score"]].drop_duplicates(
        ["signal_date", "code"], keep="last",
    )


def _signal_universe_filter(job: FactorBacktestJobOut) -> str:
    if job.universe_id == "all_a":
        return ""
    index_code = UNIVERSES[job.universe_id]["index_code"].replace("'", "''")
    return f"""
        AND entity_code IN (
            SELECT DISTINCT con_code
            FROM starlight.ad_index_constituent
            WHERE index_code = '{index_code}'
              AND in_date <= {{date_end:Date}}
              AND (out_date IS NULL OR out_date >= {{date_start:Date}})
        )
    """


def _load_benchmark(job: FactorBacktestJobOut) -> tuple[pd.DatetimeIndex, pd.Series]:
    rows = client().query(
        """
        SELECT toDate(trade_time), open
        FROM starlight.ad_market_kline_daily
        WHERE code = {code:String}
          AND toDate(trade_time) >= {date_start:Date} - INTERVAL 160 DAY
          AND toDate(trade_time) <= {date_end:Date} + INTERVAL 10 DAY
          AND open IS NOT NULL AND open > 0
        ORDER BY trade_time
        """,
        parameters={"code": job.benchmark_code, "date_start": job.date_start, "date_end": job.date_end},
    ).result_rows
    frame = pd.DataFrame(rows, columns=["date", "open"])
    if frame.empty:
        raise ValueError(f"基准 {job.benchmark_code} 缺少行情")
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.drop_duplicates("date", keep="last").set_index("date").sort_index()
    calendar = pd.DatetimeIndex(frame.index)
    returns = pd.to_numeric(frame["open"], errors="coerce").pct_change().shift(-1)
    return calendar, returns


def _apply_universe_and_sample_filters(
    job: FactorBacktestJobOut, signals: pd.DataFrame, calendar: pd.DatetimeIndex,
) -> pd.DataFrame:
    result = signals.copy()
    if job.universe_id != "all_a":
        index_code = UNIVERSES[job.universe_id]["index_code"]
        rows = client().query(
            """
            SELECT con_code, in_date, out_date
            FROM starlight.ad_index_constituent
            WHERE index_code = {index_code:String}
              AND in_date <= {date_end:Date}
              AND (out_date IS NULL OR out_date >= {date_start:Date})
            """,
            parameters={"index_code": index_code, "date_start": job.date_start, "date_end": job.date_end},
        ).result_rows
        members = pd.DataFrame(rows, columns=["code", "in_date", "out_date"])
        if members.empty:
            raise ValueError("股票池缺少历史成分")
        members["in_date"] = pd.to_datetime(members["in_date"])
        members["out_date"] = pd.to_datetime(members["out_date"]).fillna(pd.Timestamp.max.normalize())
        result = result.merge(members, on="code", how="inner")
        result = result[
            (result["signal_date"] >= result["in_date"])
            & (result["signal_date"] <= result["out_date"])
        ].drop(columns=["in_date", "out_date"])
    filters = _sample_filters(job.configuration)
    if filters["exclude_bse"]:
        result = result[~result["code"].astype(str).str.endswith(".BJ")]
    if filters["exclude_new_stocks"] or filters["exclude_delisting"]:
        basics = _load_stock_basic(sorted(set(result["code"])))
        result = result.merge(basics, on="code", how="left")
        if filters["exclude_new_stocks"]:
            signal_positions = calendar.searchsorted(result["signal_date"])
            ipo_positions = calendar.searchsorted(
                result["ipo_date"].fillna(result["signal_date"]),
            )
            result["listing_trading_days"] = signal_positions - ipo_positions
            result = result[
                result["listing_trading_days"]
                >= filters["minimum_listing_trading_days"]
            ]
        if filters["exclude_delisting"]:
            result = result[
                result["out_date"].isna()
                | (result["signal_date"] < result["out_date"])
            ]
    return result[["signal_date", "code", "score"]].drop_duplicates(
        ["signal_date", "code"], keep="last",
    )


def _load_stock_basic(codes: list[str]) -> pd.DataFrame:
    rows = client().query(
        """
        SELECT code, toDateOrNull(ipo_date), toDateOrNull(nullIf(out_date, ''))
        FROM baostock.bs_stock_basic
        WHERE type = '1' AND code IN {codes:Array(String)}
        """,
        parameters={"codes": codes},
    ).result_rows
    frame = pd.DataFrame(rows, columns=["code", "ipo_date", "out_date"])
    frame["ipo_date"] = pd.to_datetime(frame["ipo_date"])
    frame["out_date"] = pd.to_datetime(frame["out_date"])
    return frame


def _load_market(
    job: FactorBacktestJobOut, codes: list[str], calendar: pd.DatetimeIndex,
) -> pd.DataFrame:
    rows = client().query(
        """
        SELECT
            toDate(k.trade_time) AS trade_date,
            k.code,
            k.open,
            k.open * ifNull(a.backward_adj_factor, 1.0) AS adjusted_open,
            toUInt8(ifNull(s.is_st_sec, '') IN ('1','true','True')) AS is_st,
            toUInt8(ifNull(s.is_susp_sec, '') IN ('1','true','True')) AS is_suspended,
            toUInt8(ifNull(s.is_wd_sec, '') IN ('1','true','True')) AS is_withdrawal,
            s.high_limited,
            s.low_limited
        FROM starlight.ad_market_kline_daily k
        ASOF LEFT JOIN (
            SELECT code AS adjustment_code, toDate(divid_operate_date) AS factor_date,
                   toFloat64OrNull(nullIf(back_adjust_factor, '')) AS backward_adj_factor
            FROM baostock.bs_adjust_factor
            WHERE code IN {codes:Array(String)}
            ORDER BY code, factor_date
        ) a ON k.code = a.adjustment_code AND toDate(k.trade_time) >= a.factor_date
        ANY LEFT JOIN starlight.ad_history_stock_status s
          ON k.code = s.market_code AND toDate(k.trade_time) = s.trade_date
        WHERE k.code IN {codes:Array(String)}
          AND k.open > 0
          AND toDate(k.trade_time) >= {date_start:Date} - INTERVAL 160 DAY
          AND toDate(k.trade_time) <= {date_end:Date} + INTERVAL 10 DAY
        ORDER BY trade_date, code
        """,
        parameters={"codes": codes, "date_start": job.date_start, "date_end": job.date_end},
    ).result_rows
    frame = pd.DataFrame(rows, columns=[
        "date", "code", "open", "adjusted_open", "is_st", "is_suspended",
        "is_withdrawal", "high_limit", "low_limit",
    ])
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"])
    for column in ("open", "adjusted_open", "high_limit", "low_limit"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["adjusted_open"] = frame["adjusted_open"].where(
        frame["adjusted_open"] > 0
    )
    frame["has_open"] = frame["open"].notna() & (frame["open"] > 0)
    frame["buy_allowed"] = (
        frame["has_open"] & (frame["is_suspended"] == 0)
        & ~(frame["high_limit"].notna() & (frame["open"] >= frame["high_limit"] - 1e-8))
    )
    frame["sell_allowed"] = (
        frame["has_open"] & (frame["is_suspended"] == 0)
        & ~(frame["low_limit"].notna() & (frame["open"] <= frame["low_limit"] + 1e-8))
    )
    frame = frame.drop_duplicates(["date", "code"], keep="last")
    prices = frame.pivot(index="date", columns="code", values="adjusted_open").reindex(calendar).ffill()
    forward_returns = prices.shift(-1).div(prices).sub(1.0)
    result_rows = []
    indexed = frame.set_index(["date", "code"])
    for trade_date in calendar:
        if trade_date not in forward_returns.index:
            continue
        for code, value in forward_returns.loc[trade_date].dropna().items():
            state = indexed.loc[(trade_date, code)] if (trade_date, code) in indexed.index else None
            result_rows.append({
                "date": trade_date, "code": code, "forward_return": float(value),
                "buy_allowed": bool(state["buy_allowed"]) if state is not None else False,
                "sell_allowed": bool(state["sell_allowed"]) if state is not None else False,
                "is_st": int(state["is_st"]) if state is not None else 0,
                "is_withdrawal": int(state["is_withdrawal"]) if state is not None else 0,
            })
    return pd.DataFrame(result_rows)


def _build_targets(
    signals: pd.DataFrame,
    market: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    configuration: Optional[dict[str, Any]] = None,
) -> tuple[dict[pd.Timestamp, dict[str, float]], dict[pd.Timestamp, dict[str, float]], dict[pd.Timestamp, float], dict[pd.Timestamp, int]]:
    filters = _sample_filters(configuration)
    state_lookup = market.set_index(["date", "code"])
    q1: dict[pd.Timestamp, dict[str, float]] = {}
    q5: dict[pd.Timestamp, dict[str, float]] = {}
    ic: dict[pd.Timestamp, float] = {}
    counts: dict[pd.Timestamp, int] = {}
    for signal_date, group in signals.groupby("signal_date", sort=True):
        position = calendar.searchsorted(signal_date, side="right")
        if position >= len(calendar):
            continue
        execution_date = calendar[position]
        eligible = []
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
            eligible.append((str(row.code), float(row.score), float(state["forward_return"])))
        if len(eligible) < 25:
            continue
        ordered = sorted(eligible, key=lambda item: (item[1], item[0]))
        group_size = max(1, len(ordered) // 5)
        low = ordered[:group_size]
        high = ordered[-group_size:]
        q1[execution_date] = {code: 1.0 / len(low) for code, _, _ in low}
        q5[execution_date] = {code: 1.0 / len(high) for code, _, _ in high}
        score_series = pd.Series([item[1] for item in ordered])
        return_series = pd.Series([item[2] for item in ordered])
        if score_series.nunique() > 1 and return_series.nunique() > 1:
            ic[execution_date] = float(score_series.corr(return_series, method="spearman"))
        counts[execution_date] = len(ordered)
    return q1, q5, ic, counts


def _sample_filters(configuration: Optional[dict[str, Any]]) -> dict[str, Any]:
    source = configuration or {}
    filters = {
        key: source.get(key, default)
        for key, default in DEFAULT_SAMPLE_FILTERS.items()
    }
    # Jobs created before sample filters became configurable persisted the
    # original mandatory rules without these two explicit switches.
    if "exclude_new_stocks" not in source and "minimum_listing_trading_days" in source:
        filters["exclude_new_stocks"] = True
    if "exclude_limit_paused" not in source and "blocked_trades_are_carried" in source:
        filters["exclude_limit_paused"] = False
    return filters


def _simulate_quantile_portfolio(
    targets: dict[pd.Timestamp, dict[str, float]],
    market: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    buy_cost_rate: float,
    sell_cost_rate: float,
) -> dict[pd.Timestamp, PortfolioDay]:
    lookup = market.set_index(["date", "code"])
    weights: dict[str, float] = {}
    cash = 1.0
    results: dict[pd.Timestamp, PortfolioDay] = {}
    for trade_date in calendar[:-1]:
        target = targets.get(trade_date)
        old = dict(weights)
        if target is None:
            target = old
        next_weights: dict[str, float] = {}
        blocked_sells = 0
        for code, old_weight in sorted(old.items()):
            desired = float(target.get(code, 0.0))
            state = lookup.loc[(trade_date, code)] if (trade_date, code) in lookup.index else None
            can_sell = bool(state["sell_allowed"]) if state is not None else False
            if old_weight > desired and not can_sell:
                next_weights[code] = old_weight
                blocked_sells += 1
            elif desired > 0:
                next_weights[code] = min(old_weight, desired)
        budget = max(0.0, 1.0 - sum(next_weights.values()))
        blocked_buys = 0
        for code, desired in sorted(target.items()):
            current = next_weights.get(code, 0.0)
            gap = max(0.0, desired - current)
            if gap <= 0:
                continue
            state = lookup.loc[(trade_date, code)] if (trade_date, code) in lookup.index else None
            can_buy = bool(state["buy_allowed"]) if state is not None else False
            if not can_buy:
                blocked_buys += 1
                continue
            amount = min(gap, budget)
            if amount > 0:
                next_weights[code] = current + amount
                budget -= amount
        cash = max(0.0, 1.0 - sum(next_weights.values()))
        buys = sum(max(next_weights.get(code, 0.0) - old.get(code, 0.0), 0.0) for code in set(old) | set(next_weights))
        sells = sum(max(old.get(code, 0.0) - next_weights.get(code, 0.0), 0.0) for code in set(old) | set(next_weights))
        cost = buys * buy_cost_rate + sells * sell_cost_rate
        asset_growth: dict[str, float] = {}
        gross_return = 0.0
        for code, weight in next_weights.items():
            state = lookup.loc[(trade_date, code)] if (trade_date, code) in lookup.index else None
            value = float(state["forward_return"]) if state is not None else 0.0
            if not isfinite(value):
                value = 0.0
            gross_return += weight * value
            asset_growth[code] = weight * (1.0 + value)
        growth = cash + sum(asset_growth.values())
        if growth <= 0:
            raise ValueError("组合净值归零")
        weights = {code: value / growth for code, value in asset_growth.items() if value > 1e-12}
        cash = cash / growth
        results[trade_date] = PortfolioDay(
            trade_date=trade_date.date(), net_return=gross_return - cost,
            turnover=0.5 * (buys + sells), cost=cost,
            blocked_buy_count=blocked_buys, blocked_sell_count=blocked_sells,
            holdings=tuple(sorted(weights)),
        )
    return results


def _combine_daily_results(
    job: FactorBacktestJobOut,
    factor_id: str,
    q1: dict[pd.Timestamp, PortfolioDay],
    q5: dict[pd.Timestamp, PortfolioDay],
    benchmark_returns: pd.Series,
    ic_by_date: dict[pd.Timestamp, float],
    sample_counts: dict[pd.Timestamp, int],
) -> pd.DataFrame:
    rows = []
    q1_nav = q5_nav = ls_nav = benchmark_nav = 1.0
    for trade_date in sorted(set(q1) & set(q5)):
        if trade_date < pd.Timestamp(job.date_start) or trade_date > pd.Timestamp(job.date_end):
            continue
        one = q1[trade_date]
        five = q5[trade_date]
        benchmark = _safe_float(benchmark_returns.get(trade_date))
        if benchmark is None:
            continue
        long_short = five.net_return - one.net_return
        excess = five.net_return - benchmark
        q1_nav *= 1.0 + one.net_return
        q5_nav *= 1.0 + five.net_return
        ls_nav *= 1.0 + long_short
        benchmark_nav *= 1.0 + benchmark
        rows.append({
            "trade_date": trade_date.date(), "q1_return": one.net_return,
            "q5_return": five.net_return, "long_short_return": long_short,
            "benchmark_return": benchmark, "excess_return": excess,
            "q1_nav": q1_nav, "q5_nav": q5_nav, "long_short_nav": ls_nav,
            "benchmark_nav": benchmark_nav,
            "turnover": 0.5 * (one.turnover + five.turnover),
            "transaction_cost": 0.5 * (one.cost + five.cost),
            "ic": ic_by_date.get(trade_date), "sample_count": sample_counts.get(trade_date, 0),
            "blocked_buy_count": one.blocked_buy_count + five.blocked_buy_count,
            "blocked_sell_count": one.blocked_sell_count + five.blocked_sell_count,
        })
    return pd.DataFrame(rows)


def _annualized_return(values: Iterable[float]) -> Optional[float]:
    series = pd.Series(values, dtype=float).dropna()
    if series.empty:
        return None
    growth = float((1.0 + series).prod())
    if growth <= 0:
        return -1.0
    return growth ** (252.0 / len(series)) - 1.0


def _max_drawdown(nav: pd.Series) -> Optional[float]:
    clean = pd.to_numeric(nav, errors="coerce").dropna()
    if clean.empty:
        return None
    drawdown = clean / clean.cummax() - 1.0
    return _safe_float(drawdown.min())


def _safe_ratio(numerator: Any, denominator: Any) -> Optional[float]:
    left = _safe_float(numerator)
    right = _safe_float(denominator)
    return left / right if left is not None and right not in (None, 0.0) else None


def _safe_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None
