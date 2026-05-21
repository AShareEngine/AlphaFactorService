from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from factor_service import repository
from factor_service.schemas import FactorJobCreate, FactorJobOut
from factor_service.worker import run_job, run_pending_jobs


router = APIRouter(prefix="/factor-jobs", tags=["factor-jobs"])


@router.get("", response_model=list[FactorJobOut])
def list_jobs(
    factor_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[FactorJobOut]:
    return repository.list_jobs(factor_id=factor_id, status=status, limit=limit)


@router.post("", response_model=FactorJobOut)
def create_job(payload: FactorJobCreate) -> FactorJobOut:
    try:
        return repository.create_job(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/run-pending", response_model=list[FactorJobOut])
def run_pending(limit: int = Query(default=5, ge=1, le=50)) -> list[FactorJobOut]:
    try:
        return run_pending_jobs(limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{job_id}", response_model=FactorJobOut)
def get_job(job_id: str) -> FactorJobOut:
    job = repository.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.post("/{job_id}/run", response_model=FactorJobOut)
def run_one_job(job_id: str) -> FactorJobOut:
    try:
        return run_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
