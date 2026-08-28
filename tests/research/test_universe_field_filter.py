from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json

import pandas as pd
import pytest

from factor_service.research import data_binding_source
from factor_service.research.universe_field_filter import (
    normalize_universe_field_filters,
)


def _filter() -> dict:
    binding = {
        "source_type": "node",
        "source_id": "stock_daily_real",
        "source_label": "股票日线数据",
        "provider_node_id": "stock_daily_real",
        "provider_node_version": 7,
        "provider_node_version_id": "registry_node_version_test",
        "provider_node_source_hash": "a" * 64,
        "provider_node_updated_at": "2026-08-28T08:00:00+00:00",
        "field_bindings": {
            "trade_date": "trade_time",
            "instrument": "code",
            "value": "is_st",
        },
        "catalog_updated_at": "2026-08-28T08:00:00+00:00",
    }
    binding["fingerprint"] = sha256(json.dumps(
        binding,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return {
        "schema_version": "alphablocks.universe-entity-field-filter.v1",
        "kind": "entity_field",
        "entity_id": "stock",
        "asset_id": "asset_stock_daily_stock_daily_real",
        "asset_updated_at": "2026-08-28T08:00:00+00:00",
        "provider_node": "stock_daily_real",
        "field": "is_st",
        "source_field": "is_st",
        "data_type": "boolean",
        "operator": "eq",
        "missing_policy": "exclude",
        "value": False,
        "binding": binding,
    }


def test_normalize_universe_field_filter_preserves_frozen_identity() -> None:
    source = _filter()

    assert normalize_universe_field_filters([source]) == [source]


def test_normalize_universe_field_filter_rejects_binding_drift() -> None:
    source = deepcopy(_filter())
    source["binding"]["field_bindings"]["value"] = "paused"

    with pytest.raises(ValueError, match="source_field"):
        normalize_universe_field_filters([source])


def test_frozen_data_cutoff_compares_the_instant_not_iso_format() -> None:
    assert data_binding_source._same_instant(
        "2026-08-28T07:30:00+00:00",
        "2026-08-28T15:30:00+08:00",
    )
    assert not data_binding_source._same_instant(
        "2026-08-28T07:30:01+00:00",
        "2026-08-28T15:30:00+08:00",
    )


def test_bound_field_predicate_filters_observations_fail_closed(monkeypatch) -> None:
    source = _filter()
    observations = pd.DataFrame({
        "trade_date": pd.to_datetime([
            "2026-08-27", "2026-08-27", "2026-08-27",
        ]),
        "instrument": ["000001.SZ", "000002.SZ", "000003.SZ"],
    })

    def fake_query_daily_chunks(**kwargs):
        assert "filters" not in kwargs
        assert kwargs["roles"] == ("trade_date", "instrument", "value")
        assert kwargs["binding"]["field_bindings"]["value"] == "is_st"
        frame = observations.iloc[[0, 1]].copy()
        frame["value"] = ["0", "1"]
        frame.attrs["training_data_binding"] = {"query": "frozen"}
        return frame

    monkeypatch.setattr(
        data_binding_source, "_query_daily_chunks", fake_query_daily_chunks,
    )

    result = data_binding_source.load_bound_universe_filter_membership(
        object(),
        source["binding"],
        observations,
        operator="eq",
        value=False,
        data_type="boolean",
        data_cutoff="2026-08-28T15:30:00+08:00",
    )

    assert result["instrument"].tolist() == ["000001.SZ"]
    assert result.attrs["training_data_binding"] == {"query": "frozen"}


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        ("not_in", [1, 3], ["000002.SZ"]),
        ("between", [2, 3], ["000002.SZ", "000003.SZ"]),
        ("is_null", None, ["000004.SZ"]),
    ],
)
def test_bound_field_predicate_supports_generic_operators(
    monkeypatch, operator, value, expected,
) -> None:
    source = _filter()
    observations = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-08-27"] * 5),
        "instrument": [
            "000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ",
            "000005.SZ",
        ],
    })

    def fake_query_daily_chunks(**_kwargs):
        frame = observations.iloc[:4].copy()
        frame["value"] = [1, 2, 3, None]
        return frame

    monkeypatch.setattr(
        data_binding_source, "_query_daily_chunks", fake_query_daily_chunks,
    )
    result = data_binding_source.load_bound_universe_filter_membership(
        object(), source["binding"], observations,
        operator=operator, value=value, data_type="integer",
    )

    assert result["instrument"].tolist() == expected
