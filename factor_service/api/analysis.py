from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from factor_service import repository
from factor_service.analysis import (
    run_analysis_job,
    run_formula_analysis,
    run_pending_analysis_jobs,
)
from factor_service.schemas import (
    FactorAnalysisIcOut,
    FactorAnalysisJobCreate,
    FactorAnalysisJobOut,
    FactorAnalysisQuantileReturnOut,
    FactorAnalysisSummaryOut,
    FactorAnalysisTurnoverOut,
    FactorFormulaAnalysisRequest,
)


router = APIRouter(prefix="/factor-analysis", tags=["factor-analysis"])


@router.get("/jobs", response_model=list[FactorAnalysisJobOut])
def list_analysis_jobs(
    factor_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[FactorAnalysisJobOut]:
    return repository.list_analysis_jobs(factor_id=factor_id, status=status, limit=limit)


@router.post("/jobs", response_model=FactorAnalysisJobOut)
def create_analysis_job(payload: FactorAnalysisJobCreate) -> FactorAnalysisJobOut:
    try:
        return repository.create_analysis_job(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs/run-pending", response_model=list[FactorAnalysisJobOut])
def run_pending(limit: int = Query(default=3, ge=1, le=20)) -> list[FactorAnalysisJobOut]:
    try:
        return run_pending_analysis_jobs(limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/jobs/{analysis_job_id}", response_model=FactorAnalysisJobOut)
def get_analysis_job(analysis_job_id: str) -> FactorAnalysisJobOut:
    job = repository.get_analysis_job(analysis_job_id)
    if not job:
        raise HTTPException(status_code=404, detail="分析任务不存在")
    return job


@router.post("/jobs/{analysis_job_id}/run", response_model=FactorAnalysisJobOut)
def run_one_analysis_job(analysis_job_id: str) -> FactorAnalysisJobOut:
    try:
        return run_analysis_job(analysis_job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs/{analysis_job_id}/start", response_model=FactorAnalysisJobOut)
def start_one_analysis_job(
    analysis_job_id: str,
    background_tasks: BackgroundTasks,
) -> FactorAnalysisJobOut:
    """Start one pending analysis without blocking an SDK or notebook request."""
    job = repository.get_analysis_job(analysis_job_id)
    if not job:
        raise HTTPException(status_code=404, detail="分析任务不存在")
    if job.status == "pending":
        background_tasks.add_task(run_analysis_job, analysis_job_id)
    return job


@router.post("/formula")
def analyze_formula(payload: FactorFormulaAnalysisRequest) -> dict:
    """Analyze an unregistered formula without persisting factor values."""
    try:
        return run_formula_analysis(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/summary", response_model=list[FactorAnalysisSummaryOut])
def summary(analysis_job_id: str = Query(min_length=1)) -> list[FactorAnalysisSummaryOut]:
    return repository.list_analysis_summary(analysis_job_id)


@router.get("/ic", response_model=list[FactorAnalysisIcOut])
def ic(
    analysis_job_id: str = Query(min_length=1),
    period: Optional[str] = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> list[FactorAnalysisIcOut]:
    return repository.list_analysis_ic(analysis_job_id, period=period, limit=limit)


@router.get("/quantile-returns", response_model=list[FactorAnalysisQuantileReturnOut])
def quantile_returns(
    analysis_job_id: str = Query(min_length=1),
    period: Optional[str] = Query(default=None),
    limit: int = Query(default=5000, ge=1, le=20000),
) -> list[FactorAnalysisQuantileReturnOut]:
    return repository.list_analysis_quantile_returns(analysis_job_id, period=period, limit=limit)


@router.get("/turnover", response_model=list[FactorAnalysisTurnoverOut])
def turnover(
    analysis_job_id: str = Query(min_length=1),
    period: Optional[str] = Query(default=None),
    limit: int = Query(default=5000, ge=1, le=20000),
) -> list[FactorAnalysisTurnoverOut]:
    return repository.list_analysis_turnover(analysis_job_id, period=period, limit=limit)
