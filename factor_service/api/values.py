from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from factor_service import repository
from factor_service.schemas import (
    CoverageOut,
    FactorValueOut,
    FactorValueQualityOut,
    FactorValueSyncStateOut,
    FactorValueSyncStatesRequest,
)
from factor_service.worker import factor_params_hash


router = APIRouter(prefix="/factor-values", tags=["factor-values"])


@router.get("", response_model=list[FactorValueOut])
def list_values(
    factor_id: Optional[str] = Query(default=None),
    factor_version: Optional[int] = Query(default=None, ge=1),
    entity_type: Optional[str] = Query(default=None),
    entity_code: Optional[str] = Query(default=None),
    entity_codes: Optional[list[str]] = Query(default=None),
    params_hash: Optional[str] = Query(default=None),
    job_id: Optional[str] = Query(default=None),
    trade_date: Optional[date] = Query(default=None),
    date_start: Optional[date] = Query(default=None),
    date_end: Optional[date] = Query(default=None),
    date_end_exclusive: Optional[date] = Query(default=None),
    available_before: Optional[datetime] = Query(default=None),
    event_available_before: Optional[datetime] = Query(default=None),
    allow_latest: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    order_by: str = Query(default="trade_date"),
    order_dir: str = Query(default="desc"),
) -> list[FactorValueOut]:
    _validate_visibility_cutoff(
        available_before=available_before,
        event_available_before=event_available_before,
        allow_latest=allow_latest,
    )
    return repository.list_values(
        factor_id=factor_id,
        factor_version=factor_version,
        entity_type=entity_type,
        entity_code=entity_code,
        entity_codes=entity_codes,
        params_hash=params_hash,
        job_id=job_id,
        trade_date=trade_date,
        date_start=date_start,
        date_end=date_end,
        date_end_exclusive=date_end_exclusive,
        available_before=available_before,
        event_available_before=event_available_before,
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
    entity_codes: Optional[list[str]] = Query(default=None),
    params_hash: Optional[str] = Query(default=None),
    job_id: Optional[str] = Query(default=None),
    trade_date: Optional[date] = Query(default=None),
    date_start: Optional[date] = Query(default=None),
    date_end: Optional[date] = Query(default=None),
    date_end_exclusive: Optional[date] = Query(default=None),
    available_before: Optional[datetime] = Query(default=None),
    event_available_before: Optional[datetime] = Query(default=None),
    allow_latest: bool = Query(default=False),
) -> dict[str, int]:
    _validate_visibility_cutoff(
        available_before=available_before,
        event_available_before=event_available_before,
        allow_latest=allow_latest,
    )
    return {
        "count": repository.count_values(
            factor_id=factor_id,
            factor_version=factor_version,
            entity_type=entity_type,
            entity_code=entity_code,
            entity_codes=entity_codes,
            params_hash=params_hash,
            job_id=job_id,
            trade_date=trade_date,
            date_start=date_start,
            date_end=date_end,
            date_end_exclusive=date_end_exclusive,
            available_before=available_before,
            event_available_before=event_available_before,
        )
    }


def _validate_visibility_cutoff(
    *,
    available_before: Optional[datetime],
    event_available_before: Optional[datetime],
    allow_latest: bool,
) -> None:
    if available_before and event_available_before:
        raise HTTPException(
            status_code=400,
            detail="available_before 与 event_available_before 不能同时使用",
        )
    if available_before or event_available_before or allow_latest:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "读取因子值必须提供 available_before（严格时点）或 "
            "event_available_before（历史重建）；管理页面读取最新值需显式设置 "
            "allow_latest=true"
        ),
    )


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


@router.post("/sync-states", response_model=list[FactorValueSyncStateOut])
def sync_states(payload: FactorValueSyncStatesRequest) -> list[FactorValueSyncStateOut]:
    states: list[FactorValueSyncStateOut] = []
    try:
        for item in payload.items:
            factor = repository.get_factor(item.factor_id, version=item.factor_version)
            if not factor:
                raise ValueError(f"因子不存在: {item.factor_id}")
            if item.entity_type != factor.asset_id:
                raise ValueError(f"因子 {item.factor_id} 的 entity_type 与 asset_id 不一致")
            params_hash = factor_params_hash(factor, item.params)
            current = repository.coverage(
                factor_id=factor.factor_id,
                factor_version=factor.version,
                entity_type=item.entity_type,
                params_hash=params_hash,
            )
            states.append(
                FactorValueSyncStateOut(
                    **current.model_dump(),
                    factor_version=factor.version,
                    entity_type=item.entity_type,
                    params_hash=params_hash,
                )
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return states


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
