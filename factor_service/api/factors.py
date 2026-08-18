from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from factor_service import repository
from factor_service.schemas import (
    FactorCreate,
    FactorOut,
    FactorParameterIdentityOut,
    FactorUpdate,
    FactorValueSyncStatesRequest,
)
from factor_service.worker import factor_params_hash


router = APIRouter(prefix="/factors", tags=["factors"])


@router.get("", response_model=list[FactorOut])
def list_factors(
    entity_type: Optional[str] = Query(default=None),
    enabled: Optional[bool] = Query(default=None),
) -> list[FactorOut]:
    return repository.list_factors(entity_type=entity_type, enabled=enabled)


@router.post("/parameter-identities", response_model=list[FactorParameterIdentityOut])
def parameter_identities(
    payload: FactorValueSyncStatesRequest,
) -> list[FactorParameterIdentityOut]:
    requests = [(item.factor_id, item.factor_version) for item in payload.items]
    factors_by_key = repository.get_factors_for_identity(requests)
    identities: list[FactorParameterIdentityOut] = []
    try:
        for item in payload.items:
            factor = factors_by_key.get((item.factor_id, item.factor_version))
            if factor is None:
                raise ValueError(f"因子不存在: {item.factor_id}")
            if item.entity_type != factor.asset_id:
                raise ValueError(f"因子 {item.factor_id} 的 entity_type 与 asset_id 不一致")
            identities.append(FactorParameterIdentityOut(
                factor_id=factor.factor_id,
                factor_version=factor.version,
                entity_type=item.entity_type,
                params_hash=factor_params_hash(factor, item.params),
            ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return identities


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
