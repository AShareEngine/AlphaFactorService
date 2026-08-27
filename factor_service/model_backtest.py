from __future__ import annotations

from datetime import date, datetime
import json
from math import isfinite, sqrt
from typing import Any, Mapping, Optional

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
from factor_service.research.config import load_settings as load_research_settings
from factor_service.research.dataset import DatasetBuilder
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
        architecture = _architecture_configuration(job)
        date_start, date_end = _resolve_model_range(job)
        job = model_repository.update_model_backtest_job(
            backtest_job_id, date_start=date_start, date_end=date_end,
        )
        signals = (
            _load_architecture_signals(job, architecture)
            if architecture else _load_model_signals(job)
        )
        architecture_gate_audit = (
            dict(signals.attrs.get("architecture_gate_audit") or {})
            if architecture else {}
        )
        if signals.empty:
            raise ValueError("所选范围没有可用的模型或架构预测分数")
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
            "signal_source": (
                "model_architecture" if architecture else "model_version"
            ),
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
        if architecture:
            payload["architecture"] = {
                "architecture_id": architecture["architecture_id"],
                "revision": architecture["architecture_revision"],
                "fingerprint": architecture["architecture_fingerprint"],
                "pipeline_mode": architecture["pipeline_mode"],
                "merge_method": architecture["merge_method"],
                "ablation_profile": architecture["ablation_profile"],
                "ablation_label": architecture["ablation_label"],
                "enabled_engine_count": len(architecture["engines"]),
                "gate_audit": architecture_gate_audit,
                "engines": [{
                    key: engine.get(key) for key in (
                        "engine_key", "display_name", "role", "stage", "model_id",
                        "model_version", "dataset_hash", "priority",
                        "normalized_weight", "score_threshold", "top_n",
                    )
                } for engine in architecture["engines"]],
            }
            payload["walk_forward"] = _architecture_walk_forward_backtest(
                daily, dict(architecture.get("walk_forward") or {}),
            )
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
    architecture = _architecture_configuration(job)
    if architecture:
        return _resolve_architecture_range(job, architecture)
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


def _resolve_architecture_range(
    job: ModelBacktestJobOut, architecture: Mapping[str, Any],
) -> tuple[date, date]:
    models = [
        (str(item["model_id"]), int(item["model_version"]))
        for item in architecture["engines"]
    ]
    rows = client().query(
        f"""
        SELECT model_id, model_version, min(trade_date), max(trade_date)
        FROM {settings().model_database}.model_predictions_daily
        WHERE (model_id, model_version) IN
              {{models:Array(Tuple(String, UInt32))}}
        GROUP BY model_id, model_version
        """,
        parameters={"models": models},
    ).result_rows
    starts = [row[2] for row in rows if row[2] is not None]
    ends = [row[3] for row in rows if row[3] is not None]
    if (
        len(rows) != len(set(models)) or len(starts) != len(rows)
        or len(ends) != len(rows)
    ):
        raise ValueError("模型架构的预测源不完整")
    prediction_start = max(starts)
    prediction_end = min(ends)
    market_latest = client().query(
        "SELECT max(toDate(trade_time)) FROM starlight.ad_market_kline_daily WHERE code = {code:String}",
        parameters={"code": job.benchmark_code},
    ).result_rows[0][0]
    if prediction_start is None or prediction_end is None or market_latest is None:
        raise ValueError("架构预测或基准行情缺少可用范围")
    end = min(prediction_end, market_latest)
    if job.date_preset == "custom":
        start = max(job.requested_date_start, prediction_start)  # type: ignore[arg-type]
        end = min(end, job.requested_date_end)  # type: ignore[arg-type]
    else:
        offsets = {
            "3m": pd.DateOffset(months=3), "1y": pd.DateOffset(years=1),
            "3y": pd.DateOffset(years=3), "10y": pd.DateOffset(years=10),
        }
        start = max(
            (pd.Timestamp(end) - offsets[job.date_preset]).date(),
            prediction_start,
        )
    if start >= end:
        raise ValueError("架构各引擎的共同预测覆盖不足以形成回测区间")
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


def _architecture_configuration(
    job: ModelBacktestJobOut,
) -> dict[str, Any] | None:
    config = dict(job.configuration or {})
    if str(config.get("signal_source") or "") != "model_architecture":
        return None
    engines = [
        dict(item) for item in config.get("engines") or []
        if item.get("enabled") is True
    ]
    if not engines:
        raise ValueError("架构回测快照没有启用的引擎")
    merge_method = str(config.get("merge_method") or "priority")
    if merge_method not in {"priority", "weighted_score", "union"}:
        raise ValueError("架构回测快照的合并方式无效")
    pipeline_mode = str(config.get("pipeline_mode") or "flat")
    if pipeline_mode not in {"flat", "hierarchical"}:
        raise ValueError("架构回测快照的决策流程无效")
    engines.sort(key=lambda item: (int(item.get("priority") or 0), str(item.get("engine_key") or "")))
    return {
        "architecture_id": str(config.get("architecture_id") or job.model_id),
        "architecture_revision": int(config.get("architecture_revision") or job.model_version),
        "architecture_fingerprint": str(config.get("architecture_fingerprint") or ""),
        "pipeline_mode": pipeline_mode,
        "merge_method": merge_method,
        "engines": engines,
        "walk_forward": dict(config.get("walk_forward") or {}),
        "ablation_profile": str(config.get("ablation_profile") or "full"),
        "ablation_label": str(config.get("ablation_label") or "完整架构"),
    }


def _architecture_walk_forward_backtest(
    daily: pd.DataFrame, contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate a stitched architecture backtest inside its frozen OOS windows.

    The report is descriptive and never activates a model or architecture.  A
    window is considered complete only when the backtest contains its full
    declared test range, preventing a short date preset from masquerading as a
    full Walk-Forward evaluation.
    """
    base = {
        "policy": "alphablocks.architecture-walk-forward-backtest.v1",
        "eligible": contract.get("eligible") is True,
        "source_contract": dict(contract),
        "window_count": int(contract.get("window_count") or 0),
        "evaluated_window_count": 0,
        "complete_window_count": 0,
        "windows": [],
        "aggregate": {},
        "status": "not_available",
        "conclusion": str(contract.get("reason") or ""),
    }
    if contract.get("eligible") is not True or daily.empty:
        return base
    frame = daily.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    reports: list[dict[str, Any]] = []
    for position, item in enumerate(contract.get("windows") or [], start=1):
        start = pd.Timestamp(str(item.get("test_start") or ""))
        end = pd.Timestamp(str(item.get("test_end") or ""))
        window = frame[
            (frame["trade_date"] >= start) & (frame["trade_date"] <= end)
        ].copy()
        if window.empty:
            continue
        portfolio_returns = window["portfolio_return"].fillna(0.0)
        excess_returns = window["excess_return"].fillna(0.0)
        std = _safe_float(portfolio_returns.std(ddof=1))
        sharpe = None
        if std not in (None, 0.0):
            sharpe = _safe_float(
                sqrt(252.0) * portfolio_returns.mean() / float(std)
            )
        rebased_nav = (1.0 + portfolio_returns).cumprod()
        actual_start = pd.Timestamp(window["trade_date"].min())
        actual_end = pd.Timestamp(window["trade_date"].max())
        reports.append({
            "window": int(item.get("window") or position),
            "test_start": start.date().isoformat(),
            "test_end": end.date().isoformat(),
            "actual_start": actual_start.date().isoformat(),
            "actual_end": actual_end.date().isoformat(),
            "complete": actual_start <= start and actual_end >= end,
            "trading_days": int(len(window)),
            "annual_return": _annualized_return(portfolio_returns),
            "excess_annual_return": _annualized_return(excess_returns),
            "sharpe_ratio": sharpe,
            "max_drawdown": _max_drawdown(rebased_nav),
            "turnover_rate": _safe_float(window["turnover"].mean()),
        })
    complete = [item for item in reports if item["complete"]]
    excess = [
        float(item["excess_annual_return"])
        for item in complete if item.get("excess_annual_return") is not None
    ]
    base.update({
        "evaluated_window_count": len(reports),
        "complete_window_count": len(complete),
        "windows": reports,
    })
    if not excess:
        base["status"] = "insufficient"
        base["conclusion"] = "回测区间没有完整覆盖任何Walk-Forward测试窗口。"
        return base
    mean = sum(excess) / len(excess)
    variance = sum((value - mean) ** 2 for value in excess) / len(excess)
    deviation = variance ** 0.5
    positive_ratio = sum(value > 0 for value in excess) / len(excess)
    checks = [
        {
            "key": "complete_window_count", "label": "完整独立窗口",
            "actual": len(complete), "operator": ">=", "threshold": 3,
            "passed": len(complete) >= 3,
        },
        {
            "key": "mean_excess", "label": "窗口平均超额年化",
            "actual": mean, "operator": ">", "threshold": 0.0,
            "passed": mean > 0,
        },
        {
            "key": "positive_window_ratio", "label": "正超额窗口占比",
            "actual": positive_ratio, "operator": ">=", "threshold": 2.0 / 3.0,
            "passed": positive_ratio >= (2.0 / 3.0),
        },
        {
            "key": "excess_std", "label": "窗口超额年化波动",
            "actual": deviation, "operator": "<=", "threshold": 0.10,
            "passed": deviation <= 0.10,
        },
        {
            "key": "worst_window", "label": "最弱窗口超额年化",
            "actual": min(excess), "operator": ">=", "threshold": -0.05,
            "passed": min(excess) >= -0.05,
        },
    ]
    stable = all(item["passed"] for item in checks)
    base.update({
        "aggregate": {
            "excess_annual_return_mean": mean,
            "excess_annual_return_std": deviation,
            "positive_excess_window_ratio": positive_ratio,
            "worst_excess_annual_return": min(excess),
            "best_excess_annual_return": max(excess),
        },
        "checks": checks,
        "failed_checks": [item["key"] for item in checks if not item["passed"]],
        "status": "stable" if stable else "mixed",
        "conclusion": (
            "至少三个完整独立窗口且多数窗口取得正超额。"
            if stable else "跨窗口表现仍有分化，应继续检查弱势窗口和模型权重。"
        ),
    })
    return base


def _load_architecture_signals(
    job: ModelBacktestJobOut, architecture: Mapping[str, Any],
) -> pd.DataFrame:
    models = list(dict.fromkeys(
        (str(item["model_id"]), int(item["model_version"]))
        for item in architecture["engines"]
    ))
    rows = client().query(
        f"""
        SELECT trade_date, entity_type, entity_code, model_id, model_version,
               argMax(score, tuple(computed_at, inference_run_id)) AS score
        FROM {settings().model_database}.model_predictions_daily FINAL
        WHERE (model_id, model_version) IN
              {{models:Array(Tuple(String, UInt32))}}
          AND trade_date >= {{date_start:Date}} - INTERVAL 7 DAY
          AND trade_date <= {{date_end:Date}}
          AND feature_cutoff_at <= toDateTime(
              concat(toString(trade_date), ' 15:00:00'), 'Asia/Shanghai'
          )
        GROUP BY trade_date, entity_type, entity_code, model_id, model_version
        ORDER BY trade_date, model_id, model_version, entity_code
        """,
        parameters={
            "models": models, "date_start": job.date_start,
            "date_end": job.date_end,
        },
    ).result_rows
    predictions = pd.DataFrame(
        rows,
        columns=[
            "signal_date", "entity_type", "code", "model_id",
            "model_version", "score",
        ],
    )
    predictions = _expand_architecture_prediction_scopes(
        predictions, list(architecture["engines"]),
        date_start=job.date_start, date_end=job.date_end,
    )
    return _compose_architecture_signals(
        predictions, list(architecture["engines"]),
        merge_method=str(architecture["merge_method"]),
        pipeline_mode=str(architecture["pipeline_mode"]),
    )


def _expand_architecture_prediction_scopes(
    predictions: pd.DataFrame, engines: list[Mapping[str, Any]], *,
    date_start: date, date_end: date,
) -> pd.DataFrame:
    """Convert non-stock engine predictions to a stock-level gate cross-section.

    An industry model predicts SW2021 L1 index codes.  The architecture engine
    gates actual stocks by joining each score to the signal day's exact-date
    membership.  Industry history before the SW2021 cutover remains rejected.
    """
    if predictions.empty:
        return predictions.drop(columns=["entity_type"], errors="ignore")
    engine_by_model = {
        (str(item.get("model_id") or ""), int(item.get("model_version") or 0)):
            dict(item)
        for item in engines if item.get("enabled") is True
    }
    industry_keys = {
        key for key, engine in engine_by_model.items()
        if str(engine.get("prediction_scope") or "").lower() == "industry"
        or _architecture_engine_stage(engine) == "industry_gate"
    }
    frame = predictions.copy()
    frame["signal_date"] = pd.to_datetime(
        frame["signal_date"], errors="coerce",
    )
    frame["model_version"] = pd.to_numeric(
        frame["model_version"], errors="coerce",
    ).fillna(0).astype(int)
    frame["_model_key"] = list(zip(
        frame["model_id"].astype(str), frame["model_version"], strict=True,
    ))
    industry = frame[frame["_model_key"].isin(industry_keys)].copy()
    stock = frame[~frame["_model_key"].isin(industry_keys)].copy()
    if not stock.empty and (stock["entity_type"].astype(str) != "stock").any():
        raise ValueError("个股选股引擎包含非股票预测实体")
    if not industry.empty:
        if (industry["entity_type"].astype(str) != "industry").any():
            raise ValueError("行业轮动引擎必须引用industry_rotation训练目标模型")
        membership = _industry_membership_for_backtest(date_start, date_end)
        if membership.empty:
            raise ValueError("回测区间无法加载申万2021版一级行业日频映射")
        industry.rename(columns={"code": "industry_entity"}, inplace=True)
        industry = industry.merge(
            membership,
            on=["signal_date", "industry_entity"], how="inner",
        )
        industry["entity_type"] = "stock"
    result = pd.concat([stock, industry], ignore_index=True)
    return result.drop(
        columns=[
            "entity_type", "_model_key", "industry_entity",
        ], errors="ignore",
    )


def _industry_membership_for_backtest(
    date_start: date, date_end: date,
) -> pd.DataFrame:
    builder = DatasetBuilder(load_research_settings())
    universe = builder._membership(str(date_start), str(date_end))
    if universe.empty:
        return pd.DataFrame(columns=["signal_date", "industry_entity", "code"])
    membership = builder._industry_membership(
        universe[["trade_date", "instrument"]], str(date_start), str(date_end),
    )
    return membership.rename(columns={
        "trade_date": "signal_date", "instrument": "code",
    })[["signal_date", "industry_entity", "code"]]


def _compose_architecture_signals(
    predictions: pd.DataFrame, engines: list[Mapping[str, Any]], *,
    merge_method: str, pipeline_mode: str = "flat",
) -> pd.DataFrame:
    """Combine immutable engine predictions into one daily score cross-section.

    Flat mode combines every engine in parallel.  Hierarchical mode first
    intersects industry-rotation and optional risk gates, then
    applies the selected merge method only to stock-ranking engines.  Gate
    models publish their native prediction entities, while stock selectors
    already publish stock rows.  Every result remains in
    the shared ``[-1, 1]`` score contract.
    """
    columns = ["signal_date", "code", "score"]
    if predictions.empty:
        return pd.DataFrame(columns=columns)
    enabled = [dict(item) for item in engines if item.get("enabled") is True]
    enabled.sort(key=lambda item: (int(item.get("priority") or 0), str(item.get("engine_key") or "")))
    if not enabled:
        raise ValueError("架构没有启用引擎")
    if merge_method not in {"priority", "weighted_score", "union"}:
        raise ValueError("不支持的架构合并方式")
    if pipeline_mode not in {"flat", "hierarchical"}:
        raise ValueError("不支持的架构决策流程")
    for engine in enabled:
        engine["stage"] = _architecture_engine_stage(engine)
    if pipeline_mode == "hierarchical":
        stage_counts = {
            stage: sum(item["stage"] == stage for item in enabled)
            for stage in ("industry_gate", "stock_rank")
        }
        if stage_counts["stock_rank"] < 1:
            raise ValueError("分层回测快照必须包含个股引擎")
    model_keys = {
        (str(item.get("model_id") or ""), int(item.get("model_version") or 0))
        for item in enabled
    }
    frame = predictions.copy()
    frame["signal_date"] = pd.to_datetime(frame["signal_date"], errors="coerce")
    frame["model_version"] = pd.to_numeric(frame["model_version"], errors="coerce").fillna(0).astype(int)
    frame["score"] = pd.to_numeric(frame["score"], errors="coerce")
    frame = frame.dropna(subset=["signal_date", "code", "score"])
    frame["_model_key"] = list(zip(
        frame["model_id"].astype(str), frame["model_version"], strict=True,
    ))
    frame = frame[frame["_model_key"].isin(model_keys)]
    output: list[dict[str, Any]] = []
    audit_rows: list[dict[str, int]] = []
    for signal_date, day in frame.groupby("signal_date", sort=True):
        prepared: dict[str, list[tuple[str, float]]] = {}
        for engine in enabled:
            key = str(engine.get("engine_key") or "")
            source = day[
                (day["model_id"].astype(str) == str(engine.get("model_id") or ""))
                & (day["model_version"] == int(engine.get("model_version") or 0))
            ][["code", "score"]]
            source = source[
                source["score"] >= float(engine.get("score_threshold") or 0.0)
            ].sort_values(["score", "code"], ascending=[False, True])
            source = source.drop_duplicates("code", keep="first")
            if merge_method in {"priority", "union"} and pipeline_mode == "flat":
                source = source.head(int(engine.get("top_n") or 20))
            prepared[key] = [
                (str(row.code), float(row.score))
                for row in source.itertuples(index=False)
                if isfinite(float(row.score))
            ]

        selectors = enabled
        selector_prepared = prepared
        stage_audit = {"input": int(day["code"].nunique())}
        if pipeline_mode == "hierarchical":
            allowed = set(day["code"].astype(str))
            for stage, audit_key in (
                ("industry_gate", "industry_gate"),
                ("risk_gate", "risk_gate"),
            ):
                gates = [item for item in enabled if item["stage"] == stage]
                if not gates:
                    continue
                for gate in gates:
                    allowed.intersection_update(
                        code for code, _score in prepared[
                            str(gate.get("engine_key") or "")
                        ]
                    )
                stage_audit[audit_key] = len(allowed)
            selectors = [item for item in enabled if item["stage"] == "stock_rank"]
            selector_prepared = {
                str(item.get("engine_key") or ""): ([
                    (code, score) for code, score in prepared[
                        str(item.get("engine_key") or "")
                    ] if code in allowed
                ][:int(item.get("top_n") or 20)]
                    if merge_method in {"priority", "union"}
                    else [
                        (code, score) for code, score in prepared[
                            str(item.get("engine_key") or "")
                        ] if code in allowed
                    ])
                for item in selectors
            }
        combined = _merge_architecture_candidates(
            selector_prepared, selectors, merge_method=merge_method,
        )
        stage_audit["stock_rank"] = len(combined)
        audit_rows.append(stage_audit)
        output.extend({
            "signal_date": signal_date, "code": code, "score": score,
        } for code, score in combined)
    result = pd.DataFrame(output, columns=columns).sort_values(
        ["signal_date", "score", "code"], ascending=[True, False, True],
    ).reset_index(drop=True)
    result.attrs["architecture_gate_audit"] = _architecture_gate_audit(
        audit_rows, pipeline_mode=pipeline_mode,
    )
    return result


def _architecture_engine_stage(engine: Mapping[str, Any]) -> str:
    frozen = str(engine.get("stage") or "").strip()
    if frozen in {"industry_gate", "risk_gate", "stock_rank"}:
        return frozen
    return {
        "industry_rotation": "industry_gate",
        "risk_filter": "risk_gate",
    }.get(str(engine.get("role") or "stock_selection"), "stock_rank")


def _merge_architecture_candidates(
    prepared: Mapping[str, list[tuple[str, float]]],
    engines: list[Mapping[str, Any]], *, merge_method: str,
) -> list[tuple[str, float]]:
    if not engines:
        return []
    total_weight = sum(float(item.get("weight") or 0.0) for item in engines)
    if total_weight <= 0:
        raise ValueError("架构选股引擎的权重之和必须大于0")
    weights = {
        str(item.get("engine_key") or ""):
            float(item.get("weight") or 0.0) / total_weight
        for item in engines
    }
    if merge_method == "priority":
        ordered_codes: list[str] = []
        seen: set[str] = set()
        for engine in engines:
            for code, _score in prepared[str(engine.get("engine_key") or "")]:
                if code not in seen:
                    seen.add(code)
                    ordered_codes.append(code)
        size = len(ordered_codes)
        return [
            (code, 1.0 if size == 1 else 1.0 - (2.0 * index / (size - 1)))
            for index, code in enumerate(ordered_codes)
        ]
    if merge_method == "weighted_score":
        score_maps = {
            str(engine.get("engine_key") or ""):
                dict(prepared[str(engine.get("engine_key") or "")])
            for engine in engines
        }
        common = set(score_maps[next(iter(score_maps))])
        for scores in score_maps.values():
            common.intersection_update(scores)
        combined = [
            (
                code,
                max(-1.0, min(1.0, sum(
                    score_maps[str(engine.get("engine_key") or "")][code]
                    * weights[str(engine.get("engine_key") or "")]
                    for engine in engines
                ))),
            )
            for code in common
        ]
        return sorted(combined, key=lambda item: (-item[1], item[0]))
    score_lists: dict[str, list[tuple[str, float]]] = {}
    for engine in engines:
        key = str(engine.get("engine_key") or "")
        for code, score in prepared[key]:
            score_lists.setdefault(code, []).append((key, score))
    combined = []
    for code, values in score_lists.items():
        denominator = sum(weights[key] for key, _score in values)
        if denominator <= 0:
            continue
        score = sum(weights[key] * value for key, value in values) / denominator
        combined.append((code, max(-1.0, min(1.0, score))))
    return sorted(combined, key=lambda item: (-item[1], item[0]))


def _architecture_gate_audit(
    rows: list[Mapping[str, int]], *, pipeline_mode: str,
) -> dict[str, Any]:
    stages = []
    for key, label in (
        ("input", "输入股票"),
        ("industry_gate", "行业轮动门控"),
        ("risk_gate", "风险过滤"),
        ("stock_rank", "个股排序候选"),
    ):
        values = [int(item[key]) for item in rows if key in item]
        if not values:
            continue
        stages.append({
            "key": key,
            "label": label,
            "average_count": sum(values) / len(values),
            "minimum_count": min(values),
            "maximum_count": max(values),
        })
    return {
        "policy": "alphablocks.architecture-gate-audit.v1",
        "pipeline_mode": pipeline_mode,
        "signal_day_count": len(rows),
        "stages": stages,
    }


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


__all__ = [
    "_build_top_n_targets", "_compose_architecture_signals",
    "run_model_backtest_job",
]
