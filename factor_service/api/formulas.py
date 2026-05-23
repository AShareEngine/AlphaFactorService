from __future__ import annotations

from fastapi import APIRouter

from factor_service.qlib_formula import compile_qlib_formula
from factor_service.schemas import FactorFormulaValidateOut, FactorFormulaValidateRequest


router = APIRouter(prefix="/factor-formulas", tags=["factor-formulas"])


@router.post("/validate", response_model=FactorFormulaValidateOut)
def validate_formula(payload: FactorFormulaValidateRequest) -> FactorFormulaValidateOut:
    try:
        compiled = compile_qlib_formula(
            payload.expression,
            params=payload.params,
            code_column=payload.code_column,
            date_column=payload.date_column,
        )
    except ValueError as exc:
        return FactorFormulaValidateOut(
            valid=False,
            expression=payload.expression,
            error_message=str(exc),
        )
    return FactorFormulaValidateOut(
        valid=True,
        expression=payload.expression,
        required_fields=compiled.fields,
        max_window=compiled.max_window,
        compiled_sql=compiled.sql,
    )
