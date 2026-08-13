from __future__ import annotations

from datetime import date, datetime
import json
from typing import Optional
from uuid import uuid4

from factor_service.clickhouse import client, settings
from factor_service.factor_backtest import UNIVERSES
from factor_service.schemas import (
    ModelBacktestDailyOut,
    ModelBacktestJobCreate,
    ModelBacktestJobOut,
    ModelPredictionBatchIn,
    ModelPredictionOut,
)


def insert_model_predictions(payload: ModelPredictionBatchIn) -> int:
    database = settings().model_database
    now = datetime.now()
    rows = [
        [
            row.trade_date, "stock", row.entity_code, payload.model_id,
            payload.model_version, row.raw_prediction, row.rank_value,
            row.percentile, row.score, row.feature_cutoff_at, row.computed_at,
            row.source_vintage, payload.dataset_hash, payload.inference_run_id, now,
        ]
        for row in payload.rows
    ]
    client().insert(
        f"{database}.model_predictions_daily",
        rows,
        column_names=[
            "trade_date", "entity_type", "entity_code", "model_id",
            "model_version", "raw_prediction", "rank_value", "percentile",
            "score", "feature_cutoff_at", "computed_at", "source_vintage",
            "dataset_hash", "inference_run_id", "updated_at",
        ],
    )
    return len(rows)


def list_model_predictions(
    *, model_id: str, model_version: int, trade_date: Optional[date] = None,
    limit: int = 500,
) -> list[ModelPredictionOut]:
    database = settings().model_database
    condition = "AND trade_date = {trade_date:Date}" if trade_date else ""
    params = {
        "model_id": model_id, "model_version": model_version,
        "limit": max(1, min(limit, 5000)),
    }
    if trade_date:
        params["trade_date"] = trade_date
    rows = client().query(
        f"""
        SELECT trade_date, entity_code, raw_prediction, rank_value, percentile,
               score, feature_cutoff_at, computed_at, source_vintage,
               dataset_hash, inference_run_id
        FROM {database}.model_predictions_daily FINAL
        WHERE model_id = {{model_id:String}}
          AND model_version = {{model_version:UInt32}}
          {condition}
        ORDER BY trade_date DESC, score DESC, entity_code
        LIMIT {{limit:UInt32}}
        """,
        parameters=params,
    ).result_rows
    return [
        ModelPredictionOut(
            trade_date=row[0], entity_code=row[1], raw_prediction=row[2],
            rank_value=row[3], percentile=row[4], score=row[5],
            feature_cutoff_at=row[6], computed_at=row[7], source_vintage=row[8],
            dataset_hash=row[9], inference_run_id=row[10],
            model_id=model_id, model_version=model_version,
        )
        for row in rows
    ]


def create_model_backtest_job(payload: ModelBacktestJobCreate) -> ModelBacktestJobOut:
    if payload.universe_id not in UNIVERSES:
        raise ValueError("不支持的股票池")
    if payload.date_preset == "custom":
        if not payload.date_start or not payload.date_end or payload.date_start >= payload.date_end:
            raise ValueError("自定义回测必须提供有效日期范围")
    database = settings().model_database
    exists = client().query(
        f"""
        SELECT count()
        FROM {database}.model_predictions_daily
        WHERE model_id = {{model_id:String}} AND model_version = {{model_version:UInt32}}
        """,
        parameters={"model_id": payload.model_id, "model_version": payload.model_version},
    ).result_rows[0][0]
    if not exists:
        raise ValueError("模型版本没有预测结果")
    now = datetime.now()
    backtest_job_id = f"model_backtest_{uuid4().hex}"
    configuration = {
        "signal_time": "trade_date_close",
        "execution_time": "next_trade_date_open",
        "execution_price": "next_open_backward_adjusted",
        "portfolio": "top_n_equal_weight",
        "blocked_trades_are_carried": True,
        "exclude_limit_paused": False,
        "exclude_st": False,
        "exclude_new_stocks": False,
        "exclude_delisting": False,
        "exclude_bse": False,
    }
    row = [
        backtest_job_id, payload.model_id, payload.model_version,
        payload.universe_id, UNIVERSES[payload.universe_id]["benchmark"],
        payload.date_preset, payload.date_start, payload.date_end, None, None,
        payload.top_n, payload.rebalance_every, 0.0003, 0.0013,
        json.dumps(configuration, ensure_ascii=False, sort_keys=True),
        "pending", "", None, None, None, None, None, 0, "{}",
        now, None, None, now,
    ]
    client().insert(
        f"{database}.model_backtest_jobs", [row],
        column_names=_MODEL_JOB_COLUMNS,
    )
    return get_model_backtest_job(backtest_job_id)  # type: ignore[return-value]


def get_model_backtest_job(backtest_job_id: str) -> Optional[ModelBacktestJobOut]:
    database = settings().model_database
    rows = client().query(
        f"SELECT * FROM {database}.model_backtest_jobs FINAL WHERE backtest_job_id = {{id:String}} LIMIT 1",
        parameters={"id": backtest_job_id},
    ).result_rows
    return _job_from_row(rows[0]) if rows else None


def update_model_backtest_job(backtest_job_id: str, **changes) -> ModelBacktestJobOut:
    current = get_model_backtest_job(backtest_job_id)
    if current is None:
        raise ValueError("模型回测任务不存在")
    now = datetime.now()
    values = current.model_dump()
    values.update({key: value for key, value in changes.items() if value is not None})
    row = [
        current.backtest_job_id, current.model_id, current.model_version,
        current.universe_id, current.benchmark_code, current.date_preset,
        current.requested_date_start, current.requested_date_end,
        values.get("date_start"), values.get("date_end"), current.top_n,
        current.rebalance_every, current.buy_cost_rate, current.sell_cost_rate,
        json.dumps(current.configuration, ensure_ascii=False, sort_keys=True),
        values["status"], values.get("error_message", ""),
        values.get("annual_return"), values.get("excess_annual_return"),
        values.get("sharpe_ratio"), values.get("turnover_rate"),
        values.get("max_drawdown"), values.get("trading_days", 0),
        json.dumps(values.get("payload") or {}, ensure_ascii=False, sort_keys=True),
        current.created_at or now, values.get("started_at"), values.get("finished_at"), now,
    ]
    client().insert(f"{settings().model_database}.model_backtest_jobs", [row], column_names=_MODEL_JOB_COLUMNS)
    return get_model_backtest_job(backtest_job_id)  # type: ignore[return-value]


def replace_model_backtest_daily(backtest_job_id: str, rows: list[tuple]) -> None:
    database = settings().model_database
    client().command(
        f"ALTER TABLE {database}.model_backtest_daily DELETE WHERE backtest_job_id = {{id:String}} SETTINGS mutations_sync = 2",
        parameters={"id": backtest_job_id},
    )
    if rows:
        now = datetime.now()
        client().insert(
            f"{database}.model_backtest_daily",
            [list(row) + [now] for row in rows],
            column_names=[
                "backtest_job_id", "trade_date", "portfolio_return",
                "benchmark_return", "excess_return", "portfolio_nav",
                "benchmark_nav", "turnover", "transaction_cost", "sample_count",
                "holding_count", "blocked_buy_count", "blocked_sell_count",
                "holdings_json", "updated_at",
            ],
        )


def list_model_backtest_daily(backtest_job_id: str, limit: int = 5000) -> list[ModelBacktestDailyOut]:
    rows = client().query(
        f"""
        SELECT * FROM {settings().model_database}.model_backtest_daily FINAL
        WHERE backtest_job_id = {{id:String}}
        ORDER BY trade_date LIMIT {{limit:UInt32}}
        """,
        parameters={"id": backtest_job_id, "limit": max(1, min(limit, 20000))},
    ).result_rows
    return [
        ModelBacktestDailyOut(
            backtest_job_id=row[0], trade_date=row[1], portfolio_return=row[2],
            benchmark_return=row[3], excess_return=row[4], portfolio_nav=row[5],
            benchmark_nav=row[6], turnover=row[7], transaction_cost=row[8],
            sample_count=row[9], holding_count=row[10], blocked_buy_count=row[11],
            blocked_sell_count=row[12], holdings=json.loads(row[13] or "[]"),
            updated_at=row[14],
        )
        for row in rows
    ]


_MODEL_JOB_COLUMNS = [
    "backtest_job_id", "model_id", "model_version", "universe_id",
    "benchmark_code", "date_preset", "requested_date_start",
    "requested_date_end", "date_start", "date_end", "top_n",
    "rebalance_every", "buy_cost_rate", "sell_cost_rate", "configuration_json",
    "status", "error_message", "annual_return", "excess_annual_return",
    "sharpe_ratio", "turnover_rate", "max_drawdown", "trading_days",
    "payload_json", "created_at", "started_at", "finished_at", "updated_at",
]


def _job_from_row(row) -> ModelBacktestJobOut:
    return ModelBacktestJobOut(
        backtest_job_id=row[0], model_id=row[1], model_version=row[2],
        universe_id=row[3], benchmark_code=row[4], date_preset=row[5],
        requested_date_start=row[6], requested_date_end=row[7], date_start=row[8],
        date_end=row[9], top_n=row[10], rebalance_every=row[11],
        buy_cost_rate=row[12], sell_cost_rate=row[13],
        configuration=json.loads(row[14] or "{}"), status=row[15],
        error_message=row[16], annual_return=row[17], excess_annual_return=row[18],
        sharpe_ratio=row[19], turnover_rate=row[20], max_drawdown=row[21],
        trading_days=row[22], payload=json.loads(row[23] or "{}"),
        created_at=row[24], started_at=row[25], finished_at=row[26], updated_at=row[27],
    )
