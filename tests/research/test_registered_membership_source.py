from __future__ import annotations

from hashlib import sha256
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from factor_service.model_research_repository import _dataset_spec
from factor_service.research import data_binding_source as binding_source
from factor_service.research import dataset as dataset_module
from factor_service.research.dataset import DatasetBuilder
from factor_service.research.training_resource_settings import (
    INDEX_MEMBERSHIP_BINDING_ID,
    required_training_data_binding_ids,
)
from factor_service.research.universe_source import (
    normalize_registered_membership_source,
    normalize_universe_source,
)


def _source(shape: str = "interval") -> dict:
    fields = (
        {"trade_date": "trade_date", "instrument": "code"}
        if shape == "daily_snapshot"
        else {
            "instrument": "code", "in_date": "in_date",
            "out_date": "out_date",
        }
    )
    binding = {
        "binding_id": "asset_custom_pool:custom_pool_node:0",
        "source_type": "node",
        "source_id": "custom_pool_node",
        "provider_node_id": "custom_pool_node",
        "provider_node_version": 4,
        "provider_node_version_id": "registry_node_version_4",
        "provider_node_source_hash": "b" * 64,
        "membership_shape": shape,
        "field_bindings": fields,
    }
    identity = {
        "asset_id": "asset_custom_pool",
        "asset_version": 3,
        "asset_version_id": "data_asset_version_3",
        "asset_source_hash": "a" * 64,
        "membership_shape": shape,
        "binding": binding,
    }
    fingerprint = sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    return {
        "schema_version": "alphablocks.registered-membership-source.v1",
        "source_id": "asset_custom_pool",
        "source_kind": "entity_asset",
        "label": "研究股票池",
        "pit": True,
        **identity,
        "binding_fingerprint": fingerprint,
    }


def _configured_pool_source() -> dict:
    return {
        "schema_version": "alphablocks.configured-stock-pool-source.v1",
        "source_id": "csi500",
        "source_kind": "configured_stock_pool",
        "label": "中证500",
        "version": 9,
        "pit": True,
        "settings_revision": 9,
        "binding_id": "index_membership",
        "binding_fingerprint": "c" * 64,
        "selector": {
            "field_role": "index_code", "operator": "eq",
            "value": "000905.SH",
        },
        "benchmark_code": "000905.SH",
        "config_fingerprint": "d" * 64,
    }


def test_registered_source_contract_rejects_raw_asset_payload_and_tampering() -> None:
    source = _source()
    assert normalize_registered_membership_source(source) == source

    with pytest.raises(ValueError, match="原始资产字段"):
        normalize_registered_membership_source({**source, "provider_bindings": []})
    with pytest.raises(ValueError, match="fingerprint"):
        normalize_registered_membership_source({
            **source, "binding_fingerprint": "b" * 64,
        })


def test_configured_stock_pool_source_is_frozen_without_builtin_mapping() -> None:
    source = _configured_pool_source()

    assert normalize_universe_source(source) == source
    assert INDEX_MEMBERSHIP_BINDING_ID in required_training_data_binding_ids({
        "universe_id": "csi500",
        "universe_source": source,
        "sample_filters": {"minimum_listing_trading_days": 0},
    })

    spec = _dataset_spec({
        "name": "configured csi500",
        "universe_id": "csi500",
        "universe_source": source,
        "factors": [{
            "factor_id": "momentum", "factor_version": 1,
            "params_hash": "f" * 64, "params": {},
        }],
        "date_start": "2021-01-01",
        "date_end": "2025-12-31",
        "data_cutoff": "2026-01-05T15:30:00+08:00",
        "label_horizon_trading_days": 5,
        "split": {"mode": "ratio", "train": 0.7, "valid": 0.15,
                  "test": 0.15, "embargo_days": 5},
        "sample_filters": {"minimum_listing_trading_days": 0},
        "universe_field_filters": [],
        "preprocessing": {"enabled": False},
        "industry_feature": {"enabled": False},
    })

    assert spec["universe_id"] == "csi500"
    assert spec["index_code"] == "000905.SH"
    assert spec["benchmark_code"] == "000905.SH"
    assert spec["universe_source"] == source


def test_query_provenance_must_match_frozen_provider_node_version() -> None:
    binding = _source()["binding"]
    binding_source._assert_frozen_provider_version(binding, {
        "source_registry_version": 4,
        "source_registry_version_id": "registry_node_version_4",
        "source_registry_hash": "b" * 64,
    })
    with pytest.raises(ValueError, match="数据节点已变更"):
        binding_source._assert_frozen_provider_version(binding, {
            "source_registry_version": 5,
            "source_registry_version_id": "registry_node_version_5",
            "source_registry_hash": "c" * 64,
        })


def test_registered_source_does_not_require_builtin_index_binding() -> None:
    required = required_training_data_binding_ids({
        "universe_id": "asset_custom_pool",
        "universe_source": _source(),
        "sample_filters": {
            "minimum_listing_trading_days": 0,
            "exclude_st": False,
            "exclude_delisting": False,
        },
    })
    assert INDEX_MEMBERSHIP_BINDING_ID not in required


def test_dataset_spec_freezes_registered_source_as_dataset_identity() -> None:
    source = _source()
    spec = _dataset_spec({
        "name": "custom universe",
        "universe_id": source["source_id"],
        "universe_source": source,
        "factors": [{
            "factor_id": "momentum", "factor_version": 1,
            "params_hash": "f" * 64, "params": {},
        }],
        "date_start": "2021-01-01",
        "date_end": "2025-12-31",
        "data_cutoff": "2026-01-05T15:30:00+08:00",
        "label_horizon_trading_days": 5,
        "split": {"mode": "ratio", "train": 0.7, "valid": 0.15,
                  "test": 0.15, "embargo_days": 5},
        "sample_filters": {
            "minimum_listing_trading_days": 0,
            "exclude_st": False,
            "exclude_delisting": False,
        },
        "preprocessing": {"enabled": False},
        "industry_feature": {"enabled": False},
    })

    assert spec["universe_id"] == "asset_custom_pool"
    assert spec["universe_source"] == source
    assert spec["index_code"] == "000905.SH"


def test_daily_snapshot_membership_is_intersected_with_frozen_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = pd.DataFrame([
        {"trade_date": "2024-01-02", "instrument": "000001.SZ"},
        {"trade_date": "2024-01-06", "instrument": "SURVIVOR.SZ"},
    ])
    raw.attrs["training_data_binding"] = {"data_versions": ["dv-1"]}
    calls: list[dict] = []

    def query_daily(**kwargs):
        calls.append(kwargs)
        return raw

    monkeypatch.setattr(binding_source, "_query_daily_chunks", query_daily)

    result = binding_source.load_bound_registered_membership(
        SimpleNamespace(), _source("daily_snapshot"),
        pd.DatetimeIndex(["2024-01-02", "2024-01-03"]),
        date_start="2024-01-02", date_end="2024-01-03",
        data_cutoff="2025-01-02T07:00:00+00:00",
    )

    assert result[["trade_date", "instrument"]].to_dict("records") == [{
        "trade_date": pd.Timestamp("2024-01-02"),
        "instrument": "000001.SZ",
    }]
    frozen = result.attrs["training_data_binding"][
        "registered_membership_source"
    ]
    assert frozen["asset_version"] == 3
    assert frozen["membership_shape"] == "daily_snapshot"
    assert frozen["data_cutoff"] == "2025-01-02T07:00:00+00:00"
    assert calls[0]["data_cutoff"] == "2025-01-02T07:00:00+00:00"


def test_interval_membership_expands_open_end_on_trading_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intervals = pd.DataFrame([
        {
            "instrument": "000001.SZ", "in_date": "2024-01-03",
            "out_date": None,
        },
        {
            "instrument": "000002.SZ", "in_date": "2024-01-02",
            "out_date": "2024-01-02",
        },
    ])
    calls: list[dict] = []

    def query_binding(**kwargs):
        calls.append(kwargs)
        return intervals, {
            "data_version": "dv-2",
            "data_cutoff": kwargs["data_cutoff"],
        }

    monkeypatch.setattr(binding_source, "_query_binding", query_binding)

    result = binding_source.load_bound_registered_membership(
        SimpleNamespace(), _source("interval"),
        pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04"]),
        date_start="2024-01-02", date_end="2024-01-04",
        data_cutoff="2025-01-02T07:00:00+00:00",
    )

    assert result["instrument"].tolist() == [
        "000002.SZ", "000001.SZ", "000001.SZ",
    ]
    assert calls[0]["data_cutoff"] == "2025-01-02T07:00:00+00:00"


def test_dataset_builder_routes_registered_source_through_same_membership_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = DatasetBuilder.__new__(DatasetBuilder)
    builder.settings = SimpleNamespace()
    calendar_binding = {"binding_id": "trading_calendar"}
    monkeypatch.setattr(
        dataset_module, "frozen_data_binding",
        lambda _bindings, binding_id: (
            calendar_binding if binding_id == "trading_calendar" else None
        ),
    )
    monkeypatch.setattr(
        dataset_module, "load_bound_trading_calendar",
        lambda *_args, **_kwargs: pd.DatetimeIndex([
            "2024-01-02", "2024-01-03",
        ]),
    )
    calls: list[dict] = []

    def load_custom(_settings, source, calendar, **kwargs):
        calls.append({"source": source, **kwargs})
        frame = pd.DataFrame([
            {"trade_date": calendar[0], "instrument": "000001.SZ"},
        ])
        frame.attrs["training_data_binding"] = {
            "registered_membership_source": {"asset_id": source["asset_id"]},
        }
        return frame

    monkeypatch.setattr(
        dataset_module, "load_bound_registered_membership", load_custom,
    )
    result = builder._membership(
        "2024-01-02", "2024-01-03",
        universe_id="asset_custom_pool", index_code="000905.SH",
        universe_source=_source(),
        sample_filters={
            "minimum_listing_trading_days": 0,
            "exclude_st": False,
            "exclude_delisting": False,
        },
        data_bindings={"bindings": {"trading_calendar": calendar_binding}},
        data_cutoff="2025-01-02T07:00:00+00:00",
    )

    assert calls == [{
        "source": _source(),
        "date_start": "2024-01-02",
        "date_end": "2024-01-03",
        "data_cutoff": "2025-01-02T07:00:00+00:00",
    }]
    assert result["instrument"].tolist() == ["000001.SZ"]
    assert result.attrs["universe_filter_steps"][0]["rule_id"] == (
        "membership_source"
    )
