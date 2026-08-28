from types import SimpleNamespace

import pandas as pd

from factor_service.research.data_binding_source import (
    _observation_chunks,
    _query_binding,
    load_bound_industry_membership,
    load_bound_stock_daily,
    load_bound_stock_status,
    load_bound_trading_calendar,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        data_sdk_api_base_url="http://data-sdk.test/api/data-sdk",
        data_sdk_query_timeout_seconds=30,
        data_sdk_query_concurrency=1,
    )


def _binding() -> dict:
    return {
        "binding_id": "stock_industry_one_hot",
        "settings_revision": 9,
        "fingerprint": "abc123",
        "source_type": "entity_asset",
        "source_id": "asset_industry_membership",
        "provider_node_id": "industry_membership_weight_real",
        "field_bindings": {
            "trade_date": "trade_date",
            "instrument": "con_code",
            "industry_code": "index_code",
            "industry_name": "level1_name",
            "industry_level": "level_type",
            "weight": "weight",
        },
        "industry_level_value": "1",
    }


def test_observation_chunks_ignore_array_valued_dataframe_attrs() -> None:
    observations = pd.DataFrame({
        "trade_date": ["2026-01-05", "2026-01-06"],
        "instrument": ["000001.SZ", "600000.SH"],
    })
    observations.attrs["source_rows"] = pd.Series([1, 2]).to_numpy()

    chunks = _observation_chunks(observations)

    assert len(chunks) == 1
    assert chunks[0].to_dict("records") == [
        {"trade_date": "2026-01-05", "instrument": "000001.SZ"},
        {"trade_date": "2026-01-06", "instrument": "600000.SH"},
    ]
    assert chunks[0].attrs == {}


def test_query_binding_forwards_and_verifies_frozen_data_cutoff(
    monkeypatch,
) -> None:
    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {
                "ok": True,
                "columns": ["trade_date", "con_code"],
                "rows": [["2024-01-02", "000001.SZ"]],
                "provenance": {
                    "data_cutoff": "2025-01-02T07:00:00+00:00",
                },
            }

    def fake_post(url, *, json, timeout):
        calls.append((url, json, timeout))
        return Response()

    monkeypatch.setattr(
        "factor_service.research.data_binding_source.requests.post",
        fake_post,
    )

    _, provenance = _query_binding(
        settings=_settings(),
        binding=_binding(),
        roles=("trade_date", "instrument"),
        date_start="2024-01-02",
        date_end_exclusive="2024-01-03",
        data_cutoff="2025-01-02T07:00:00+00:00",
    )

    assert calls[0][1]["data_cutoff"] == "2025-01-02T07:00:00+00:00"
    assert provenance["data_cutoff"] == "2025-01-02T07:00:00+00:00"


def test_load_bound_industry_membership_uses_frozen_database_binding(
    monkeypatch,
) -> None:
    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {
                "ok": True,
                "columns": [
                    "trade_date",
                    "con_code",
                    "index_code",
                    "level1_name",
                    "level_type",
                    "weight",
                ],
                "rows": [
                    ["2026-01-05", "000001.SZ", "801780", "银行", 1, 1.0],
                    ["2026-01-05", "000001.SZ", "000000", "无效", 1, 0.0],
                    ["2026-01-05", "600000.SH", "801780", "银行", 1, 1.0],
                ],
                "provenance": {
                    "data_version": "dv-20260105",
                    "schema_version": "sv-3",
                },
            }

    def fake_post(url, *, json, timeout):
        calls.append((url, json, timeout))
        return Response()

    monkeypatch.setattr(
        "factor_service.research.data_binding_source.requests.post",
        fake_post,
    )
    observations = pd.DataFrame({
        "trade_date": ["2026-01-05", "2026-01-05"],
        "instrument": ["000001.SZ", "600000.SH"],
    })

    result = load_bound_industry_membership(
        _settings(), observations, _binding(),
    )

    assert result.to_dict("records") == [
        {
            "trade_date": pd.Timestamp("2026-01-05"),
            "instrument": "000001.SZ",
            "industry_entity": "801780",
            "industry_name": "银行",
            "industry_weight": 1.0,
        },
        {
            "trade_date": pd.Timestamp("2026-01-05"),
            "instrument": "600000.SH",
            "industry_entity": "801780",
            "industry_name": "银行",
            "industry_weight": 1.0,
        },
    ]
    url, payload, timeout = calls[0]
    assert url == "http://data-sdk.test/api/data-sdk/query"
    assert timeout == 30
    assert payload["source_kind"] == "node"
    assert payload["source_id"] == "industry_membership_weight_real"
    assert payload["params"] == {
        "start": "2026-01-05",
        "end": "2026-01-06",
    }
    assert payload["query"]["projection"] == [
        {"kind": "field", "field": "trade_date"},
        {"kind": "field", "field": "con_code"},
        {"kind": "field", "field": "index_code"},
        {"kind": "field", "field": "level1_name"},
        {"kind": "field", "field": "level_type"},
        {"kind": "field", "field": "weight"},
    ]
    predicate = payload["query"]["filter"]
    assert all(
        item.get("op") != "gt"
        for item in predicate.get("items", [predicate])
    )
    assert result.attrs["training_data_binding"] == {
        "binding_id": "stock_industry_one_hot",
        "settings_revision": 9,
        "fingerprint": "abc123",
        "source_type": "entity_asset",
        "source_id": "asset_industry_membership",
        "provider_node_id": "industry_membership_weight_real",
        "query_count": 1,
        "data_versions": ["dv-20260105"],
        "schema_versions": ["sv-3"],
    }


def test_core_bound_readers_query_configured_provider_fields(monkeypatch) -> None:
    calls = []

    class Response:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def fake_post(_url, *, json, timeout):
        calls.append((json, timeout))
        fields = [item["field"] for item in json["query"]["projection"]]
        if fields == ["session_date"]:
            rows = [["2026-01-05"], ["2026-01-06"]]
        elif "adj_price" in fields:
            rows = [["2026-01-05", "000001.SZ", 10.5]]
        else:
            rows = [["2026-01-05", "000001.SZ", "1", "0", "0"]]
        return Response({"ok": True, "columns": fields, "rows": rows})

    monkeypatch.setattr(
        "factor_service.research.data_binding_source.requests.post",
        fake_post,
    )
    calendar_binding = {
        **_binding(),
        "binding_id": "trading_calendar",
        "provider_node_id": "calendar_provider",
        "field_bindings": {"trade_date": "session_date"},
    }
    daily_binding = {
        **_binding(),
        "binding_id": "stock_daily_training",
        "provider_node_id": "daily_provider",
        "field_bindings": {
            "trade_date": "session_date",
            "instrument": "ticker",
            "adjusted_close": "adj_price",
        },
    }
    status_binding = {
        **_binding(),
        "binding_id": "stock_status",
        "provider_node_id": "status_provider",
        "field_bindings": {
            "trade_date": "session_date",
            "instrument": "ticker",
            "is_st": "st_flag",
            "is_delisting": "delist_flag",
            "is_suspended": "suspend_flag",
        },
    }

    calendar = load_bound_trading_calendar(
        _settings(), calendar_binding, "2026-01-05", "2026-01-06",
    )
    daily = load_bound_stock_daily(
        _settings(), daily_binding, ["000001.SZ"],
        "2026-01-05", "2026-01-05",
    )
    status = load_bound_stock_status(
        _settings(), status_binding,
        daily[["trade_date", "instrument"]],
    )

    assert calendar.tolist() == [
        pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06"),
    ]
    assert daily.iloc[0]["adjusted_close"] == 10.5
    assert status.iloc[0]["is_st"] == 1
    assert {call[0]["source_id"] for call in calls} == {
        "calendar_provider", "daily_provider", "status_provider",
    }
