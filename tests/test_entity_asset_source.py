from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from factor_service.entity_asset_source import staged_entity_asset_source


class FakeClickHouseClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.inserts: list[tuple[str, list[list[object]], list[str]]] = []

    def command(self, sql: str) -> None:
        self.commands.append(sql)

    def insert(self, table: str, rows, *, column_names) -> None:
        self.inserts.append((table, list(rows), list(column_names)))


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
