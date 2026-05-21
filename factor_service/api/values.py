from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Query

from factor_service import repository
from factor_service.schemas import CoverageOut, FactorValueOut


router = APIRouter(prefix="/factor-values", tags=["factor-values"])


@router.get("", response_model=list[FactorValueOut])
def list_values(
    factor_id: Optional[str] = Query(default=None),
    entity_type: Optional[str] = Query(default=None),
    entity_code: Optional[str] = Query(default=None),
    trade_date: Optional[date] = Query(default=None),
    date_start: Optional[date] = Query(default=None),
    date_end: Optional[date] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
) -> list[FactorValueOut]:
    return repository.list_values(
        factor_id=factor_id,
        entity_type=entity_type,
        entity_code=entity_code,
        trade_date=trade_date,
        date_start=date_start,
        date_end=date_end,
        limit=limit,
    )


@router.get("/coverage", response_model=CoverageOut)
def coverage(
    factor_id: str = Query(min_length=1),
    date_start: Optional[date] = Query(default=None),
    date_end: Optional[date] = Query(default=None),
) -> CoverageOut:
    return repository.coverage(factor_id=factor_id, date_start=date_start, date_end=date_end)
