from __future__ import annotations

from datetime import date

import pytest

from factor_service.schemas import FactorOut
from factor_service.worker import (
    ComputePlan,
    PostprocessConfig,
    _build_postprocessed_sql,
    _formula_params,
    _cleanup_superseded_values,
    factor_params_hash,
    _postprocess_config,
)


def factor(*, output_type: str = "number", processing=None) -> FactorOut:
    params = {"window": 20}
    if processing is not None:
        params["data_processing"] = processing
    return FactorOut(
        factor_id="demo",
        version=1,
        label="Demo",
        output_type=output_type,
        frequency="daily",
        entity_type="stock",
        asset_id="stock",
        source_node_id="stock_daily_real",
        params=params,
        expression="Mean($close, $window)",
    )


def test_continuous_factor_defaults_to_quantile_winsorized_zscore():
    config = _postprocess_config(factor())

    assert config == PostprocessConfig(
        winsorize="quantile",
        standardize="zscore",
        neutralize=(),
    )


def test_boolean_factor_keeps_binary_score():
    config = _postprocess_config(factor(output_type="boolean"))
    sql = _build_postprocessed_sql(
        "SELECT trade_date, entity_code, raw_value FROM source",
        output_type="boolean",
        processing=config,
    )

    assert config.standardize == "none"
    assert "toFloat64(raw_value) AS score" in sql
    assert "ORDER BY raw_value DESC" in sql


def test_rank_standardization_uses_percentile_as_score():
    config = _postprocess_config(factor(processing={
        "winsorize": "none",
        "standardize": "rank",
        "neutralize": [],
    }))
    sql = _build_postprocessed_sql(
        "SELECT trade_date, entity_code, raw_value FROM source",
        output_type="number",
        processing=config,
    )

    assert "toFloat64(percentile) AS score" in sql
    assert "percent_rank()" in sql


def test_neutralization_is_rejected_until_exposures_are_bound():
    with pytest.raises(ValueError, match="不能执行中性化"):
        _postprocess_config(factor(processing={
            "winsorize": "mad",
            "standardize": "zscore",
            "neutralize": ["industry"],
        }))


def test_processing_metadata_is_not_part_of_formula_params_hash_input():
    item = factor(processing={
        "winsorize": "mad",
        "standardize": "zscore",
        "neutralize": [],
    })
    item.params["weighting"] = "equal"
    item.param_schema = {
        "window": {"type": "integer", "default": 20}
    }

    assert _formula_params(item, {"window": 30}) == {"window": 30}


def test_factor_params_hash_matches_effective_default_params():
    item = factor()
    item.param_schema = {
        "window": {"type": "integer", "default": 20}
    }

    assert factor_params_hash(item, {}) == factor_params_hash(item, {"window": 20})


def test_cleanup_only_removes_older_batches_in_exact_scope(monkeypatch):
    calls = []

    class FakeClient:
        def command(self, sql, parameters):
            calls.append((sql, parameters))

    monkeypatch.setattr("factor_service.worker.client", lambda: FakeClient())
    plan = ComputePlan(
        sql="SELECT 1",
        params={
            "factor_id": "mean_amount",
            "factor_version": 1,
            "entity_type": "stock",
            "params_hash": "abc",
            "job_id": "new_job",
        },
        date_start=date(2026, 1, 1),
        date_end=date(2026, 1, 31),
        params_hash="abc",
    )

    _cleanup_superseded_values(plan)

    sql, params = calls[0]
    assert "job_id != {job_id:String}" in sql
    assert "mutations_sync = 2" in sql
    assert params["job_id"] == "new_job"
    assert params["params_hash"] == "abc"
