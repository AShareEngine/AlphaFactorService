from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest

from factor_service.entity_asset_source import FactorSourceBinding
from factor_service.schemas import FactorOut
from factor_service.worker import (
    ComputePlan,
    PostprocessConfig,
    _build_postprocessed_sql,
    _formula_params,
    _cleanup_superseded_values,
    build_factor_query_plan,
    factor_query_source,
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


def factor_source_settings():
    return SimpleNamespace(
        source_database="starlight",
        stock_daily_table="stock_daily_factor_source",
        stock_code_column="code",
        stock_date_column="trade_time",
        stock_basic_table="bs_stock_basic",
        stock_basic_type_column="type",
        stock_basic_stock_type_value="1",
        clickhouse_database="ab_factor",
        entity_asset_api_base_url="http://alphablocks/api/data-sdk",
        entity_asset_query_timeout_seconds=120,
        entity_asset_query_concurrency=4,
    )


def test_factor_query_source_keeps_physical_daily_fast_path(monkeypatch):
    monkeypatch.setattr(
        "factor_service.worker.settings",
        factor_source_settings,
    )
    monkeypatch.setattr(
        "factor_service.worker._resolve_date_range",
        lambda *args: (date(2024, 1, 2), date(2024, 1, 3)),
    )
    monkeypatch.setattr(
        "factor_service.worker._source_columns",
        lambda *args: {"code", "trade_time", "close"},
    )
    monkeypatch.setattr(
        "factor_service.worker.staged_entity_asset_source",
        lambda **kwargs: pytest.fail("已有字段不应触发实体资产暂存"),
    )

    with factor_query_source(
        factor(),
        overrides={},
        date_start=date(2024, 1, 2),
        date_end=date(2024, 1, 3),
        job_id="job-fast",
    ) as binding:
        assert binding.database == "starlight"
        assert binding.table == "stock_daily_factor_source"
        assert binding.date_start == date(2024, 1, 2)
        assert binding.date_end == date(2024, 1, 3)


def test_factor_query_source_stages_composite_daily_fields_when_missing(monkeypatch):
    item = factor()
    item.expression = "Mean($roe, $window)"
    captured = {}

    @contextmanager
    def fake_staged_source(**kwargs):
        captured.update(kwargs)
        yield FactorSourceBinding(
            database="ab_factor",
            table="factor_entity_asset_stage_test",
            code_column="code",
            date_column="trade_time",
            source_vintage="entity-asset:stock/daily@test",
            date_start=date(2024, 1, 3),
            date_end=date(2024, 1, 3),
        )

    monkeypatch.setattr(
        "factor_service.worker.settings",
        factor_source_settings,
    )
    monkeypatch.setattr(
        "factor_service.worker._resolve_date_range",
        lambda *args: (date(2024, 1, 3), date(2024, 1, 3)),
    )
    monkeypatch.setattr(
        "factor_service.worker._source_columns",
        lambda *args: {"code", "trade_time", "close"},
    )
    monkeypatch.setattr(
        "factor_service.worker._source_trading_dates",
        lambda *args: [date(2024, 1, 2), date(2024, 1, 3)],
    )
    monkeypatch.setattr(
        "factor_service.worker.staged_entity_asset_source",
        fake_staged_source,
    )
    fake_client = object()
    monkeypatch.setattr("factor_service.worker.client", lambda: fake_client)

    with factor_query_source(
        item,
        overrides={},
        date_start=date(2024, 1, 3),
        date_end=date(2024, 1, 3),
        job_id="job-composite",
    ) as binding:
        assert binding.table == "factor_entity_asset_stage_test"

    assert captured["db_client"] is fake_client
    assert captured["entity_id"] == "stock"
    assert captured["fields"] == ["roe"]
    assert captured["trading_dates"] == [date(2024, 1, 2), date(2024, 1, 3)]


def test_factor_query_plan_reads_staged_source_but_keeps_stock_universe(monkeypatch):
    item = factor()
    item.expression = "Mean($roe, $window)"
    binding = FactorSourceBinding(
        database="ab_factor",
        table="factor_entity_asset_stage_test",
        code_column="code",
        date_column="trade_time",
        source_vintage="entity-asset:stock/daily@test",
        date_start=date(2024, 1, 3),
        date_end=date(2024, 1, 3),
    )
    monkeypatch.setattr(
        "factor_service.worker.settings",
        factor_source_settings,
    )
    monkeypatch.setattr(
        "factor_service.worker._ensure_source_columns",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "factor_service.worker._ensure_source_has_rows",
        lambda *args: None,
    )

    plan = build_factor_query_plan(
        item,
        overrides={},
        entity_type="stock",
        date_start=date(2024, 1, 3),
        date_end=date(2024, 1, 3),
        job_id="job-composite",
        source_binding=binding,
    )

    assert "FROM ab_factor.factor_entity_asset_stage_test" in plan.sql
    assert "FROM starlight.bs_stock_basic" in plan.sql
    assert plan.params["source_vintage"] == (
        "entity-asset:stock/daily@test#job-composite"
    )
