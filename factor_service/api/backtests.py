from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from factor_service import repository
from factor_service.clickhouse import client
from factor_service.factor_backtest import UNIVERSES, run_factor_backtest_job
from factor_service.schemas import (
    FactorBacktestDailyOut,
    FactorBacktestJobCreate,
    FactorBacktestJobOut,
    FactorBacktestSummaryOut,
)


router = APIRouter(prefix="/factor-backtests", tags=["factor-backtests"])

UNIVERSE_LABELS = {
    "csi300": "沪深300",
    "csi500": "中证500",
    "csi800": "中证800",
    "csi1000": "中证1000",
    "all_a": "全A",
}


@router.get("/universes", response_model=list[dict[str, Any]])
def list_universes() -> list[dict[str, Any]]:
    codes = [item["benchmark"] for item in UNIVERSES.values()]
    rows = client().query(
        """
        SELECT code, min(toDate(trade_time)), max(toDate(trade_time)), count()
        FROM starlight.ad_market_kline_daily
        WHERE code IN {codes:Array(String)}
        GROUP BY code
        """,
        parameters={"codes": codes},
    ).result_rows
    ranges = {
        str(code): {"date_start": date_start, "date_end": date_end, "rows": int(count)}
        for code, date_start, date_end, count in rows
    }
    return [
        {
            "universe_id": universe_id,
            "label": UNIVERSE_LABELS[universe_id],
            "index_code": config["index_code"],
            "benchmark_code": config["benchmark"],
            **ranges.get(config["benchmark"], {"date_start": None, "date_end": None, "rows": 0}),
        }
        for universe_id, config in UNIVERSES.items()
    ]


@router.get("/jobs", response_model=list[FactorBacktestJobOut])
def list_jobs(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[FactorBacktestJobOut]:
    return repository.list_factor_backtest_jobs(status=status, limit=limit)


@router.post("/jobs", response_model=FactorBacktestJobOut)
def create_job(payload: FactorBacktestJobCreate) -> FactorBacktestJobOut:
    try:
        return repository.create_factor_backtest_job(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/jobs/{backtest_job_id}", response_model=FactorBacktestJobOut)
def get_job(backtest_job_id: str) -> FactorBacktestJobOut:
    job = repository.get_factor_backtest_job(backtest_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="因子回测任务不存在")
    return job


@router.post("/jobs/{backtest_job_id}/start", response_model=FactorBacktestJobOut)
def start_job(
    backtest_job_id: str,
    background_tasks: BackgroundTasks,
) -> FactorBacktestJobOut:
    job = repository.get_factor_backtest_job(backtest_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="因子回测任务不存在")
    if job.status == "pending":
        background_tasks.add_task(run_factor_backtest_job, backtest_job_id)
    return job


@router.post("/jobs/{backtest_job_id}/run", response_model=FactorBacktestJobOut)
def run_job(backtest_job_id: str) -> FactorBacktestJobOut:
    try:
        return run_factor_backtest_job(backtest_job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/jobs/{backtest_job_id}/summary", response_model=list[FactorBacktestSummaryOut])
def list_summary(backtest_job_id: str) -> list[FactorBacktestSummaryOut]:
    if repository.get_factor_backtest_job(backtest_job_id) is None:
        raise HTTPException(status_code=404, detail="因子回测任务不存在")
    return repository.list_factor_backtest_summary(backtest_job_id)


@router.get(
    "/jobs/{backtest_job_id}/factors/{factor_id}/daily",
    response_model=list[FactorBacktestDailyOut],
)
def list_daily(
    backtest_job_id: str,
    factor_id: str,
    limit: int = Query(default=5000, ge=1, le=20000),
) -> list[FactorBacktestDailyOut]:
    if repository.get_factor_backtest_job(backtest_job_id) is None:
        raise HTTPException(status_code=404, detail="因子回测任务不存在")
    return repository.list_factor_backtest_daily(backtest_job_id, factor_id, limit=limit)
