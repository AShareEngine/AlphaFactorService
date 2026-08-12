from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from factor_service import repository
from factor_service.schemas import FactorCreate, FactorOut, FactorUpdate


router = APIRouter(prefix="/factors", tags=["factors"])


@router.get("", response_model=list[FactorOut])
def list_factors(
    entity_type: Optional[str] = Query(default=None),
    enabled: Optional[bool] = Query(default=None),
) -> list[FactorOut]:
    return repository.list_factors(entity_type=entity_type, enabled=enabled)


@router.post("", response_model=FactorOut)
def create_factor(payload: FactorCreate) -> FactorOut:
    try:
        return repository.create_factor(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{factor_id}", response_model=FactorOut)
def get_factor(
    factor_id: str,
    version: Optional[int] = Query(default=None, ge=1),
) -> FactorOut:
    factor = repository.get_factor(factor_id, version=version)
    if not factor:
        raise HTTPException(status_code=404, detail="因子不存在")
    return factor


@router.put("/{factor_id}", response_model=FactorOut)
def update_factor(factor_id: str, payload: FactorUpdate) -> FactorOut:
    try:
        return repository.update_factor(factor_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{factor_id}", response_model=FactorOut)
def disable_factor(factor_id: str) -> FactorOut:
    try:
        return repository.disable_factor(factor_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
