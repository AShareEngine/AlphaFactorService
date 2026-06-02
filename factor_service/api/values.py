from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Query

from factor_service import repository
from factor_service.schemas import CoverageOut, FactorValueOut, FactorValueQualityOut


router = APIRouter(prefix="/factor-values", tags=["factor-values"])


@router.get("", response_model=list[FactorValueOut])
def list_values(
    factor_id: Optional[str] = Query(default=None),
    factor_version: Optional[int] = Query(default=None, ge=1),
    entity_type: Optional[str] = Query(default=None),
    entity_code: Optional[str] = Query(default=None),
    params_hash: Optional[str] = Query(default=None),
    job_id: Optional[str] = Query(default=None),
    trade_date: Optional[date] = Query(default=None),
    date_start: Optional[date] = Query(default=None),
    date_end: Optional[date] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    order_by: str = Query(default="trade_date"),
    order_dir: str = Query(default="desc"),
) -> list[FactorValueOut]:
    return repository.list_values(
        factor_id=factor_id,
        factor_version=factor_version,
        entity_type=entity_type,
        entity_code=entity_code,
        params_hash=params_hash,
        job_id=job_id,
        trade_date=trade_date,
        date_start=date_start,
        date_end=date_end,
        limit=limit,
        offset=offset,
        order_by=order_by,
        order_dir=order_dir,
    )


@router.get("/count")
def count_values(
    factor_id: Optional[str] = Query(default=None),
    factor_version: Optional[int] = Query(default=None, ge=1),
    entity_type: Optional[str] = Query(default=None),
    entity_code: Optional[str] = Query(default=None),
    params_hash: Optional[str] = Query(default=None),
    job_id: Optional[str] = Query(default=None),
    trade_date: Optional[date] = Query(default=None),
    date_start: Optional[date] = Query(default=None),
    date_end: Optional[date] = Query(default=None),
) -> dict[str, int]:
    return {
        "count": repository.count_values(
            factor_id=factor_id,
            factor_version=factor_version,
            entity_type=entity_type,
            entity_code=entity_code,
            params_hash=params_hash,
            job_id=job_id,
            trade_date=trade_date,
            date_start=date_start,
            date_end=date_end,
        )
    }


@router.get("/latest-date")
def latest_date(
    factor_id: str = Query(min_length=1),
    factor_version: Optional[int] = Query(default=None, ge=1),
    entity_type: Optional[str] = Query(default=None),
    params_hash: Optional[str] = Query(default=None),
    job_id: Optional[str] = Query(default=None),
    date_start: Optional[date] = Query(default=None),
    date_end: Optional[date] = Query(default=None),
) -> dict[str, object]:
    return {
        "factor_id": factor_id,
        "entity_type": entity_type,
        "trade_date": repository.latest_value_date(
            factor_id=factor_id,
            factor_version=factor_version,
            entity_type=entity_type,
            params_hash=params_hash,
            job_id=job_id,
            date_start=date_start,
            date_end=date_end,
        ),
    }


@router.get("/coverage", response_model=CoverageOut)
def coverage(
    factor_id: str = Query(min_length=1),
    factor_version: Optional[int] = Query(default=None, ge=1),
    entity_type: Optional[str] = Query(default=None),
    params_hash: Optional[str] = Query(default=None),
    job_id: Optional[str] = Query(default=None),
    date_start: Optional[date] = Query(default=None),
    date_end: Optional[date] = Query(default=None),
) -> CoverageOut:
    return repository.coverage(
        factor_id=factor_id,
        factor_version=factor_version,
        entity_type=entity_type,
        params_hash=params_hash,
        job_id=job_id,
        date_start=date_start,
        date_end=date_end,
    )


@router.get("/quality", response_model=FactorValueQualityOut)
def quality(
    factor_id: str = Query(min_length=1),
    factor_version: Optional[int] = Query(default=None, ge=1),
    entity_type: Optional[str] = Query(default=None),
    params_hash: Optional[str] = Query(default=None),
    job_id: Optional[str] = Query(default=None),
    date_start: Optional[date] = Query(default=None),
    date_end: Optional[date] = Query(default=None),
) -> FactorValueQualityOut:
    return repository.value_quality(
        factor_id=factor_id,
        factor_version=factor_version,
        entity_type=entity_type,
        params_hash=params_hash,
        job_id=job_id,
        date_start=date_start,
        date_end=date_end,
    )
