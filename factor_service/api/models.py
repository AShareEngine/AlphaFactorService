from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from factor_service import model_repository
from factor_service.model_backtest import run_model_backtest_job
from factor_service.schemas import (
    ModelBacktestDailyOut,
    ModelBacktestJobCreate,
    ModelBacktestJobOut,
    ModelPredictionBatchIn,
    ModelPredictionOut,
)


router = APIRouter(tags=["models"])


@router.post("/model-predictions/batches")
def insert_predictions(payload: ModelPredictionBatchIn) -> Dict[str, Any]:
    return {"ok": True, "inserted": model_repository.insert_model_predictions(payload)}


@router.get("/model-predictions", response_model=list[ModelPredictionOut])
def list_predictions(
    model_id: str = Query(min_length=1),
    model_version: int = Query(ge=1),
    trade_date: Optional[date] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
) -> list[ModelPredictionOut]:
    return model_repository.list_model_predictions(
        model_id=model_id, model_version=model_version,
        trade_date=trade_date, limit=limit,
    )


@router.post("/model-backtests/jobs", response_model=ModelBacktestJobOut)
def create_model_backtest(payload: ModelBacktestJobCreate) -> ModelBacktestJobOut:
    try:
        return model_repository.create_model_backtest_job(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/model-backtests/jobs/{backtest_job_id}", response_model=ModelBacktestJobOut)
def get_model_backtest(backtest_job_id: str) -> ModelBacktestJobOut:
    job = model_repository.get_model_backtest_job(backtest_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="模型回测任务不存在")
    return job


@router.post("/model-backtests/jobs/{backtest_job_id}/start", response_model=ModelBacktestJobOut)
def start_model_backtest(backtest_job_id: str, background_tasks: BackgroundTasks) -> ModelBacktestJobOut:
    job = model_repository.get_model_backtest_job(backtest_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="模型回测任务不存在")
    if job.status == "pending":
        background_tasks.add_task(run_model_backtest_job, backtest_job_id)
    return job


@router.post("/model-backtests/jobs/{backtest_job_id}/run", response_model=ModelBacktestJobOut)
def run_model_backtest(backtest_job_id: str) -> ModelBacktestJobOut:
    return run_model_backtest_job(backtest_job_id)


@router.get(
    "/model-backtests/jobs/{backtest_job_id}/daily",
    response_model=list[ModelBacktestDailyOut],
)
def list_model_backtest_daily(
    backtest_job_id: str,
    limit: int = Query(default=5000, ge=1, le=20000),
) -> list[ModelBacktestDailyOut]:
    if model_repository.get_model_backtest_job(backtest_job_id) is None:
        raise HTTPException(status_code=404, detail="模型回测任务不存在")
    return model_repository.list_model_backtest_daily(backtest_job_id, limit=limit)
