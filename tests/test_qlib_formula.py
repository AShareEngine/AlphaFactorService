from __future__ import annotations

import pytest

from factor_service.qlib_formula import compile_qlib_formula
from factor_service.api.formulas import validate_formula
from factor_service.repository import _validated_factor_payload, _value_conditions
from factor_service.schemas import FactorCreate, FactorFormulaValidateRequest


def compile_sql(expression: str, *, window: int = 20):
    return compile_qlib_formula(
        expression,
        params={"window": window},
        code_column="code",
        date_column="trade_date",
    )


def test_compile_qlib_mean_expression():
    compiled = compile_sql("Mean($close, $window)")

    assert compiled.fields == ["close"]
    assert compiled.max_window == 20
    assert "avg(close) OVER" in compiled.sql
    assert "ROWS BETWEEN 19 PRECEDING AND CURRENT ROW" in compiled.sql


def test_compile_old_asset_field_syntax_as_qlib_compatible_alias():
    compiled = compile_sql("mean($asset.volume, $window)")

    assert compiled.fields == ["volume"]
    assert "avg(volume) OVER" in compiled.sql


def test_compile_formula_with_element_functions():
    compiled = compile_sql("Sum((($close - $open) / NullIf($high - $low, 0)) * $volume, $window)")

    assert compiled.fields == ["close", "high", "low", "open", "volume"]
    assert "nullIf((high - low), 0)" in compiled.sql
    assert "sum(" in compiled.sql


def test_compile_period_return():
    compiled = compile_sql("PeriodReturn($close, $window)")

    assert compiled.fields == ["close"]
    assert compiled.max_window == 21
    assert "lagInFrame(close, 20)" in compiled.sql


def test_compile_first_true_condition():
    compiled = compile_sql("FirstTrue(And(Gt($high_limited, 0), Ge($close, $high_limited)), $window)")

    assert compiled.fields == ["close", "high_limited"]
    assert "sum(" in compiled.sql
    assert "ROWS BETWEEN 19 PRECEDING AND 1 PRECEDING" in compiled.sql


def test_compile_expanded_window_functions():
    cases = {
        "EMA($close, $window)": "toNullable(close)",
        "WMA($close, $window)": "toNullable(close)",
        "Rank($close, $window)": "<= toNullable(close)",
        "Quantile($close, $window, 0.8)": "quantile(0.8)(close)",
        "IdxMax($high, $window)": "arrayMax",
        "IdxMin($low, $window)": "arrayMin",
        "Slope($close, $window)": "nullIf",
        "Rsquare($close, $window)": "nullIf",
        "Resi($close, $window)": "toNullable(close)",
    }

    for expression, expected_sql in cases.items():
        compiled = compile_sql(expression)
        assert compiled.max_window == 20
        assert expected_sql in compiled.sql


def test_compile_qlib_pair_element_functions():
    cases = {
        "Greater($close, $open)": "greatest(close, open)",
        "Less($close, $open)": "least(close, open)",
        "Add($close, $open)": "(close + open)",
        "Sub($close, $open)": "(close - open)",
        "Mul($close, $volume)": "(close * volume)",
        "Div($close, NullIf($open, 0))": "(close / nullIf(open, 0))",
    }

    for expression, expected_sql in cases.items():
        compiled = compile_sql(expression)
        assert expected_sql in compiled.sql


def test_factor_payload_validation_syncs_required_fields():
    payload = FactorCreate(
        factor_id="demo",
        label="Demo",
        expression="Mean($turnover_rate, $window)",
        params={"window": 20},
        required_fields=[],
    )

    validated = _validated_factor_payload(payload)

    assert validated.required_fields == ["turnover_rate"]


def test_factor_payload_validation_rejects_bad_expression():
    payload = FactorCreate(
        factor_id="bad",
        label="Bad",
        expression="Mean($close, )",
        params={"window": 20},
    )

    with pytest.raises(ValueError, match="表达式不合法"):
        _validated_factor_payload(payload)


def test_cross_instrument_qlib_functions_are_explicitly_unsupported():
    with pytest.raises(ValueError, match="暂未支持"):
        compile_sql("Mask($close, $is_st)")


def test_formula_validate_api_returns_compiled_preview():
    result = validate_formula(FactorFormulaValidateRequest(
        expression="Mean($close, $window)",
        params={"window": 10},
    ))

    assert result.valid is True
    assert result.required_fields == ["close"]
    assert result.max_window == 10
    assert "avg(close) OVER" in result.compiled_sql


def test_formula_validate_api_returns_structured_error():
    result = validate_formula(FactorFormulaValidateRequest(
        expression="Mean($close, )",
        params={"window": 10},
    ))

    assert result.valid is False
    assert result.error_message


def test_value_queries_default_to_latest_factor_version():
    conditions, params = _value_conditions(factor_id="demo")

    assert params["factor_id"] == "demo"
    assert "factor_id = {factor_id:String}" in conditions
    assert any("SELECT max(version)" in condition for condition in conditions)


def test_value_queries_allow_explicit_factor_version():
    conditions, params = _value_conditions(factor_id="demo", factor_version=2)

    assert params["factor_version"] == 2
    assert "factor_version = {factor_version:UInt32}" in conditions
    assert not any("SELECT max(version)" in condition for condition in conditions)
