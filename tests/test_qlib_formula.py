from __future__ import annotations

from datetime import datetime

import pytest

from factor_service.qlib_formula import compile_qlib_formula
from factor_service.api.formulas import validate_formula
from factor_service.repository import (
    _normalize_factor_params,
    _validated_factor_payload,
    _validated_param_schema,
    _value_conditions,
)
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


def test_compile_stock_fear_proxy_expression():
    compiled = compile_qlib_formula(
        "$rv_weight * Std($pct_chg, $vol_window) "
        "+ $downside_weight * Power(Mean(Power(Less($pct_chg, 0), 2), $vol_window), 0.5) "
        "+ $loss_weight * Greater(-100 * PeriodReturn($close, $return_window), 0) "
        "+ $volume_weight * $volume_scale "
        "* Greater($volume / NullIf(Mean($volume, $volume_window), 0) - 1, 0)",
        params={
            "vol_window": 20,
            "return_window": 5,
            "volume_window": 20,
            "rv_weight": 0.35,
            "downside_weight": 0.3,
            "loss_weight": 0.2,
            "volume_weight": 0.15,
            "volume_scale": 10,
        },
        code_column="code",
        date_column="trade_date",
    )

    assert compiled.fields == ["close", "pct_chg", "volume"]
    assert compiled.max_window == 20
    assert "stddevSamp(pct_chg) OVER" in compiled.sql
    assert "avg(pow(least(pct_chg, 0), 2)) OVER" in compiled.sql
    assert "lagInFrame(close, 5)" in compiled.sql
    assert "avg(volume) OVER" in compiled.sql


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
        asset_id="stock",
        source_node_id="stock_daily_real",
    )

    validated = _validated_factor_payload(payload)

    assert validated.required_fields == ["turnover_rate"]
    assert validated.param_schema == {
        "window": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10000,
            "default": 20,
        }
    }


def test_factor_processing_metadata_is_not_treated_as_job_parameter():
    payload = FactorCreate(
        factor_id="processed_demo",
        label="Processed Demo",
        expression="Mean($amount, $window)",
        params={
            "window": 20,
            "data_processing": {
                "winsorize": "quantile",
                "standardize": "zscore",
                "neutralize": [],
            },
            "weighting": "equal",
        },
        asset_id="stock",
        source_node_id="stock_daily_real",
    )

    validated = _validated_factor_payload(payload)

    assert list(validated.param_schema) == ["window"]


def test_factor_payload_validation_rejects_bad_expression():
    payload = FactorCreate(
        factor_id="bad",
        label="Bad",
        expression="Mean($close, )",
        params={"window": 20},
    )

    with pytest.raises(ValueError, match="表达式不合法"):
        _validated_factor_payload(payload)


def test_factor_parameter_schema_and_job_overrides_are_strict():
    schema = _validated_param_schema(
        {
            "window": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
            }
        },
        {"window": 20},
    )

    assert _normalize_factor_params(
        schema,
        {"window": 20},
        {"window": 30},
    ) == {"window": 30}
    with pytest.raises(ValueError, match="未声明参数"):
        _normalize_factor_params(
            schema,
            {"window": 20},
            {"unknown": 1},
        )
    with pytest.raises(ValueError, match="大于 maximum"):
        _normalize_factor_params(
            schema,
            {"window": 20},
            {"window": 101},
        )
    with pytest.raises(ValueError, match="default"):
        _validated_param_schema(
            {
                "window": {
                    "type": "integer",
                    "default": 10,
                }
            },
            {"window": 20},
        )


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


def test_value_queries_use_actual_compute_time_for_strict_cutoff():
    cutoff = datetime(2024, 1, 3, 15, 0)

    conditions, params = _value_conditions(available_before=cutoff)

    assert "available_at <= {available_before:DateTime}" in conditions
    assert not any("event_available_at" in condition for condition in conditions)
    assert params["available_before"] == cutoff


def test_value_queries_require_explicit_event_cutoff_for_reconstruction():
    cutoff = datetime(2024, 1, 3, 15, 0)

    conditions, params = _value_conditions(event_available_before=cutoff)

    assert (
        "event_available_at <= {event_available_before:DateTime}"
        in conditions
    )
    assert not any(
        condition.startswith("available_at <=")
        for condition in conditions
    )
    assert params["event_available_before"] == cutoff
