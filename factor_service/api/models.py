from __future__ import annotations

from datetime import date, datetime
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
    ModelSignalOut,
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


@router.get("/model-signals", response_model=list[ModelSignalOut])
def list_signals(
    model_id: str = Query(min_length=1),
    model_version: int = Query(ge=1),
    trade_date: date = Query(),
    top_n: int = Query(default=20, ge=1, le=500),
) -> list[ModelSignalOut]:
    return model_repository.list_model_signals(
        model_id=model_id, model_version=model_version,
        trade_date=trade_date, top_n=top_n,
    )


@router.post("/model-paper/snapshot")
def paper_snapshot(payload: dict[str, Any]) -> dict:
    try:
        return model_repository.model_paper_snapshot(
            model_id=str(payload.get("model_id") or ""),
            model_version=int(payload.get("model_version") or 0),
            execution_date=date.fromisoformat(str(payload.get("execution_date") or "")),
            current_codes=[str(item) for item in list(payload.get("current_codes") or [])],
            top_n=int(payload.get("top_n") or 20),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/model-inference/availability")
def inference_availability(payload: dict[str, Any]) -> dict:
    try:
        requested_trade_date = payload.get("trade_date")
        if requested_trade_date:
            requested_trade_date = date.fromisoformat(str(requested_trade_date))
        data_cutoff = payload.get("data_cutoff")
        if data_cutoff:
            data_cutoff = datetime.fromisoformat(str(data_cutoff).replace("Z", "+00:00"))
        return model_repository.model_inference_availability(
            factors=list(payload.get("factors") or []),
            requested_trade_date=requested_trade_date,
            data_cutoff=data_cutoff,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/model-inference/trading-dates")
def inference_trading_dates(payload: dict[str, Any]) -> dict:
    try:
        after_date = date.fromisoformat(str(payload.get("after_date") or ""))
        before_date = payload.get("before_date")
        cutoff = payload.get("data_cutoff")
        dates = model_repository.model_inference_dates(
            factors=list(payload.get("factors") or []),
            after_date=after_date,
            before_date=date.fromisoformat(str(before_date)) if before_date else None,
            data_cutoff=datetime.fromisoformat(str(cutoff).replace("Z", "+00:00")) if cutoff else None,
            limit=int(payload.get("limit") or 20),
        )
        return {"dates": dates, "count": len(dates)}
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
