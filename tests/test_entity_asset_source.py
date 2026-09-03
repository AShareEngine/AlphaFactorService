from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from factor_service.entity_asset_source import (
    ENTITY_ASSET_QUERY_MAX_ATTEMPTS,
    _fetch_daily_asset_rows,
    _materialize_range_stage,
    staged_entity_asset_source,
)
from factor_service.research.errors import RetryableJobError


class FakeClickHouseClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.inserts: list[tuple[str, list[list[object]], list[str]]] = []

    def command(self, sql: str) -> None:
        self.commands.append(sql)

    def insert(self, table: str, rows, *, column_names) -> None:
        self.inserts.append((table, list(rows), list(column_names)))

    def query(self, _sql: str, *, parameters=None):
        assert parameters == {
            "database": "ab_factor",
            "table": "factor_entity_asset_stage_abcd",
        }
        return SimpleNamespace(result_rows=[(1,)])


def test_staged_entity_asset_source_uses_authorized_daily_composite_query(monkeypatch) -> None:
    requests = []

    def fake_post(url, *, json, timeout):
        requests.append((url, json, timeout))
        requested_date = json["params"]["date"]
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "ok": True,
                "columns": ["code", "date", "roe"],
                "rows": [["000001.SZ", requested_date, 0.125]],
                "provenance": {
                    "data_version": "data-v1",
                    "schema_version": "schema-v1",
                    "provider_nodes": [
                        "stock_daily_real",
                        "fundamentals_pit_real",
                    ],
                },
            },
        )

    monkeypatch.setattr("factor_service.entity_asset_source.requests.post", fake_post)
    db = FakeClickHouseClient()
    with staged_entity_asset_source(
        db_client=db,
        database="ab_factor",
        api_base_url="http://alphablocks/api/data-sdk",
        timeout_seconds=30,
        concurrency=2,
        entity_id="stock",
        fields=["roe"],
        trading_dates=[date(2024, 1, 2), date(2024, 1, 3)],
        date_start=date(2024, 1, 3),
        date_end=date(2024, 1, 3),
        job_id="job-1",
    ) as binding:
        assert binding.database == "ab_factor"
        assert binding.table.startswith("factor_entity_asset_stage_")
        assert binding.code_column == "code"
        assert binding.date_column == "trade_time"
        assert binding.date_start == date(2024, 1, 3)
        assert binding.date_end == date(2024, 1, 3)
        assert binding.source_vintage.startswith(
            "entity-asset:stock/daily@2024-01-02:2024-01-03#"
        )

    assert len(requests) == 2
    assert all(item[0].endswith("/api/data-sdk/query") for item in requests)
    assert {
        field["field"]
        for field in requests[0][1]["query"]["projection"]
    } == {"code", "date", "roe"}
    assert len(db.inserts) == 2
    assert db.inserts[0][2] == ["trade_time", "code", "roe"]
    assert isinstance(db.inserts[0][1][0][0], date)
    assert sum("DROP TABLE IF EXISTS" in item for item in db.commands) == 2
    assert any("CREATE TABLE" in item and "roe Nullable(Float64)" in item for item in db.commands)


def test_daily_asset_query_retries_transient_gateway_failure(monkeypatch) -> None:
    responses = iter(
        [
            SimpleNamespace(
                status_code=503,
                json=lambda: {
                    "ok": False,
                    "error": {
                        "code": "data_execution_failed",
                        "message": "upstream read timed out",
                        "retryable": True,
                    },
                },
            ),
            SimpleNamespace(
                status_code=503,
                json=lambda: {
                    "ok": False,
                    "error": {
                        "code": "data_execution_failed",
                        "message": "connection broken",
                        "retryable": True,
                    },
                },
            ),
            SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "ok": True,
                    "columns": ["code", "date", "roe"],
                    "rows": [["000001.SZ", "2024-01-02", 0.125]],
                    "provenance": {},
                },
            ),
        ]
    )
    calls = []
    delays = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return next(responses)

    monkeypatch.setattr("factor_service.entity_asset_source.requests.post", fake_post)
    monkeypatch.setattr("factor_service.entity_asset_source.time.sleep", delays.append)

    rows, _provenance = _fetch_daily_asset_rows(
        api_base_url="http://alphablocks/api/data-sdk",
        timeout_seconds=30,
        entity_id="stock",
        fields=["roe"],
        trade_date=date(2024, 1, 2),
    )

    assert rows == [[date(2024, 1, 2), "000001.SZ", 0.125]]
    assert len(calls) == 3
    assert delays == [0.5, 1.0]


def test_range_stage_sends_one_frozen_request_and_returns_managed_binding(
    monkeypatch,
) -> None:
    calls = []

    def fake_post(url, *, json, timeout):
        calls.append((url, json, timeout))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "ok": True,
                "binding": {
                    "database": "ab_factor",
                    "table": "factor_entity_asset_stage_abcd",
                    "code_column": "code",
                    "date_column": "trade_time",
                    "source_vintage": "entity-asset:stock/daily@v1",
                },
                "stage": {"reused": False},
            },
        )

    monkeypatch.setattr("factor_service.entity_asset_source.requests.post", fake_post)
    binding = _materialize_range_stage(
        db_client=FakeClickHouseClient(),
        database="ab_factor",
        api_base_url="http://alphablocks/api/data-sdk",
        timeout_seconds=30,
        entity_id="stock",
        fields=["roe", "close_adj"],
        trading_dates=[date(2024, 1, 2), date(2024, 1, 3)],
        date_start=date(2024, 1, 3),
        date_end=date(2024, 1, 3),
        stage_key="dataset-hash:chunk-1",
        data_cutoff="2025-01-01T00:00:00+00:00",
    )

    assert binding is not None
    assert binding.managed_stage is True
    assert binding.table == "factor_entity_asset_stage_abcd"
    assert len(calls) == 1
    assert calls[0][0].endswith("/internal/entity-asset-stages")
    assert calls[0][1]["trading_dates"] == ["2024-01-02", "2024-01-03"]
    assert calls[0][1]["data_cutoff"] == "2025-01-01T00:00:00+00:00"
    assert calls[0][2] == 1800.0


def test_range_stage_falls_back_when_old_gateway_has_no_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        "factor_service.entity_asset_source.requests.post",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=404,
            json=lambda: {"ok": False},
        ),
    )

    binding = _materialize_range_stage(
        db_client=FakeClickHouseClient(),
        database="ab_factor",
        api_base_url="http://alphablocks/api/data-sdk",
        timeout_seconds=30,
        entity_id="stock",
        fields=["roe"],
        trading_dates=[date(2024, 1, 2)],
        date_start=date(2024, 1, 2),
        date_end=date(2024, 1, 2),
        stage_key="dataset-hash:chunk-1",
        data_cutoff="2025-01-01T00:00:00+00:00",
    )

    assert binding is None


def test_daily_asset_query_exhaustion_is_retryable_job_error(monkeypatch) -> None:
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            status_code=503,
            json=lambda: {
                "ok": False,
                "error": {
                    "code": "query_timeout",
                    "message": "read timed out",
                    "retryable": True,
                },
            },
        )

    monkeypatch.setattr("factor_service.entity_asset_source.requests.post", fake_post)
    monkeypatch.setattr("factor_service.entity_asset_source.time.sleep", lambda _value: None)

    with pytest.raises(RetryableJobError, match="2024-01-02"):
        _fetch_daily_asset_rows(
            api_base_url="http://alphablocks/api/data-sdk",
            timeout_seconds=30,
            entity_id="stock",
            fields=["roe"],
            trade_date=date(2024, 1, 2),
        )

    assert len(calls) == ENTITY_ASSET_QUERY_MAX_ATTEMPTS


def test_daily_asset_query_does_not_retry_permanent_failure(monkeypatch) -> None:
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            status_code=400,
            json=lambda: {
                "ok": False,
                "error": {
                    "code": "field_not_found",
                    "message": "field does not exist",
                    "retryable": False,
                },
            },
        )

    monkeypatch.setattr("factor_service.entity_asset_source.requests.post", fake_post)

    with pytest.raises(ValueError, match="field does not exist"):
        _fetch_daily_asset_rows(
            api_base_url="http://alphablocks/api/data-sdk",
            timeout_seconds=30,
            entity_id="stock",
            fields=["missing_field"],
            trade_date=date(2024, 1, 2),
        )

    assert len(calls) == 1


def test_staged_entity_asset_source_rejects_non_numeric_formula_values(monkeypatch) -> None:
    monkeypatch.setattr(
        "factor_service.entity_asset_source.requests.post",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            json=lambda: {
                "ok": True,
                "columns": ["code", "date", "holder_name"],
                "rows": [["000001.SZ", "2024-01-02", "某股东"]],
                "provenance": {},
            },
        ),
    )
    db = FakeClickHouseClient()
    with pytest.raises(ValueError, match="当前数值公式不能计算"):
        with staged_entity_asset_source(
            db_client=db,
            database="ab_factor",
            api_base_url="http://alphablocks/api/data-sdk",
            timeout_seconds=30,
            concurrency=1,
            entity_id="stock",
            fields=["holder_name"],
            trading_dates=[date(2024, 1, 2)],
            date_start=date(2024, 1, 2),
            date_end=date(2024, 1, 2),
            job_id="job-2",
        ):
            pass

    assert sum("DROP TABLE IF EXISTS" in item for item in db.commands) == 2
    assert db.inserts == []


def test_staged_entity_asset_source_uses_unique_table_per_invocation(monkeypatch) -> None:
    monkeypatch.setattr(
        "factor_service.entity_asset_source.requests.post",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            json=lambda: {
                "ok": True,
                "columns": ["code", "date", "roe"],
                "rows": [["000001.SZ", "2024-01-02", 0.125]],
                "provenance": {},
            },
        ),
    )
    db = FakeClickHouseClient()
    tables = []
    kwargs = {
        "db_client": db,
        "database": "ab_factor",
        "api_base_url": "http://alphablocks/api/data-sdk",
        "timeout_seconds": 30,
        "concurrency": 1,
        "entity_id": "stock",
        "fields": ["roe"],
        "trading_dates": [date(2024, 1, 2)],
        "date_start": date(2024, 1, 2),
        "date_end": date(2024, 1, 2),
        "job_id": "shared-job-name",
    }

    with staged_entity_asset_source(**kwargs) as first:
        tables.append(first.table)
    with staged_entity_asset_source(**kwargs) as second:
        tables.append(second.table)

    assert tables[0] != tables[1]
