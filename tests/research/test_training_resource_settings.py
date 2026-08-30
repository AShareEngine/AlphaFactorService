import pytest

from factor_service.research.training_resource_settings import (
    BINDING_DEFINITIONS,
    INDEX_MEMBERSHIP_BINDING_ID,
    INDUSTRY_FEATURE_BINDING_ID,
    SECURITY_MASTER_BINDING_ID,
    STOCK_DAILY_BINDING_ID,
    STOCK_STATUS_BINDING_ID,
    TRADING_CALENDAR_BINDING_ID,
    TRAINING_DATA_BINDING_SCHEMA_VERSION,
    configured_stock_pool_sources,
    frozen_training_data_binding,
    frozen_training_data_bindings,
    normalize_frozen_training_data_binding,
    normalize_training_resource_settings,
    required_training_data_binding_ids,
    training_data_binding_ready,
)


def _configured_binding() -> dict:
    return {
        "enabled": True,
        "source_type": "node",
        "source_id": "industry_membership_weight_real",
        "source_label": "行业成分与权重",
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
        "catalog_updated_at": "2026-08-27T00:00:00Z",
    }


def _legacy_entity_asset_binding() -> dict:
    return {
        **_configured_binding(),
        "source_type": "entity_asset",
        "source_id": "asset_industry_membership",
    }


def test_training_data_binding_defaults_are_canonical() -> None:
    settings = normalize_training_resource_settings(None)

    assert settings["schema_version"] == TRAINING_DATA_BINDING_SCHEMA_VERSION
    binding = settings["bindings"][INDUSTRY_FEATURE_BINDING_ID]
    assert binding["enabled"] is False
    assert binding["source_type"] == "node"
    assert binding["source_id"] == ""
    assert not training_data_binding_ready(binding)
    assert len(binding["fingerprint"]) == 64
    assert settings["stock_pools"] == []


def test_named_stock_pools_are_configured_from_index_membership_binding() -> None:
    definition = BINDING_DEFINITIONS[INDEX_MEMBERSHIP_BINDING_ID]
    settings = normalize_training_resource_settings({
        "bindings": {
            INDEX_MEMBERSHIP_BINDING_ID: {
                "enabled": True,
                "source_type": "node",
                "source_id": "index_constituent_real",
                "source_label": "指数成分关系",
                "provider_node_id": "index_constituent_real",
                "field_bindings": {
                    role["id"]: role["hints"][0]
                    for role in definition["roles"]
                },
                "catalog_updated_at": "",
            },
        },
        "stock_pools": [{
            "id": "csi500",
            "label": "中证500",
            "selector_value": "000905.SH",
            "benchmark_code": "000905.SH",
            "enabled": True,
        }],
    })
    settings["revision"] = 9

    sources = configured_stock_pool_sources(settings)

    assert len(sources) == 1
    assert sources[0]["source_id"] == "csi500"
    assert sources[0]["version"] == 9
    assert sources[0]["selector"] == {
        "field_role": "index_code", "operator": "eq",
        "value": "000905.SH",
    }
    assert sources[0]["binding_id"] == INDEX_MEMBERSHIP_BINDING_ID
    assert sources[0]["available"] is True
    assert len(sources[0]["config_fingerprint"]) == 64


def test_retired_market_cap_binding_is_removed_from_legacy_settings() -> None:
    settings = normalize_training_resource_settings({
        "bindings": {
            "market_cap_pit": {"enabled": True},
            STOCK_DAILY_BINDING_ID: {
                "field_bindings": {
                    "trade_date": "trade_date",
                    "instrument": "instrument",
                    "adjusted_close": "adjusted_close",
                    "close": "close",
                },
            },
        },
    })

    assert "market_cap_pit" not in settings["bindings"]
    assert "close" not in settings["bindings"][STOCK_DAILY_BINDING_ID][
        "field_bindings"
    ]


def test_stock_daily_optional_float_cap_role_upgrades_old_fingerprint() -> None:
    original = {
        "enabled": True,
        "source_type": "node",
        "source_id": "stock_daily_real",
        "source_label": "股票日线",
        "provider_node_id": "stock_daily_real",
        "field_bindings": {
            "trade_date": "trade_time",
            "instrument": "code",
            "adjusted_close": "close_adj",
        },
        "catalog_updated_at": "",
        "fingerprint": "legacy-fingerprint",
    }

    settings = normalize_training_resource_settings({
        "bindings": {STOCK_DAILY_BINDING_ID: original},
    })

    binding = settings["bindings"][STOCK_DAILY_BINDING_ID]
    assert binding["field_bindings"]["float_market_cap"] == ""
    assert len(binding["fingerprint"]) == 64
    assert binding["fingerprint"] != original["fingerprint"]


def test_industry_binding_is_normalized_fingerprinted_and_frozen() -> None:
    settings = normalize_training_resource_settings({
        "bindings": {INDUSTRY_FEATURE_BINDING_ID: _configured_binding()},
    })
    settings["revision"] = 7
    binding = settings["bindings"][INDUSTRY_FEATURE_BINDING_ID]

    assert training_data_binding_ready(binding)
    frozen = frozen_training_data_binding(settings)
    assert frozen["binding_id"] == INDUSTRY_FEATURE_BINDING_ID
    assert frozen["settings_revision"] == 7
    assert normalize_frozen_training_data_binding(frozen) == frozen


def test_legacy_active_entity_asset_binding_is_upgraded_to_provider_node() -> None:
    settings = normalize_training_resource_settings({
        "schema_version": "alphablocks.model-training-data-bindings.v2",
        "bindings": {
            INDUSTRY_FEATURE_BINDING_ID: _legacy_entity_asset_binding(),
        },
    })
    binding = settings["bindings"][INDUSTRY_FEATURE_BINDING_ID]

    assert settings["schema_version"] == TRAINING_DATA_BINDING_SCHEMA_VERSION
    assert binding["source_type"] == "node"
    assert binding["source_id"] == "industry_membership_weight_real"
    assert binding["provider_node_id"] == binding["source_id"]


def test_historical_frozen_entity_asset_binding_remains_readable() -> None:
    frozen = normalize_frozen_training_data_binding({
        **_legacy_entity_asset_binding(),
        "binding_id": INDUSTRY_FEATURE_BINDING_ID,
        "settings_revision": 6,
    })

    assert frozen["source_type"] == "entity_asset"
    assert frozen["source_id"] == "asset_industry_membership"
    assert frozen["provider_node_id"] == "industry_membership_weight_real"


@pytest.mark.parametrize("source", [
    {"bindings": {"unknown": {}}},
    {"bindings": {INDUSTRY_FEATURE_BINDING_ID: {
        **_configured_binding(), "source_type": "execution_node",
    }}},
    {"bindings": {INDUSTRY_FEATURE_BINDING_ID: {
        **_configured_binding(), "source_type": "entity_asset",
    }}},
    {"bindings": {INDUSTRY_FEATURE_BINDING_ID: {
        **_configured_binding(), "provider_node_id": "",
    }}},
    {"bindings": {INDUSTRY_FEATURE_BINDING_ID: {
        **_configured_binding(),
        "field_bindings": {
            **_configured_binding()["field_bindings"],
            "trade_date": "unsafe.field",
        },
    }}},
])
def test_invalid_training_data_bindings_are_rejected(source) -> None:
    with pytest.raises(ValueError):
        normalize_training_resource_settings(source)


def test_required_bindings_follow_universe_filters_and_target() -> None:
    stock = required_training_data_binding_ids({
        "universe_id": "csi500",
        "sample_filters": {
            "minimum_listing_trading_days": 60,
            "exclude_st": True,
            "exclude_delisting": True,
        },
    })

    assert stock == [
        STOCK_DAILY_BINDING_ID,
        TRADING_CALENDAR_BINDING_ID,
        SECURITY_MASTER_BINDING_ID,
        INDEX_MEMBERSHIP_BINDING_ID,
        STOCK_STATUS_BINDING_ID,
    ]
    assert "market_cap_pit" not in BINDING_DEFINITIONS
    assert INDUSTRY_FEATURE_BINDING_ID in required_training_data_binding_ids({
        "universe_id": "all_a", "research_target": "industry_rotation",
    })


def test_all_configured_bindings_freeze_under_one_revision() -> None:
    bindings = {}
    for binding_id, definition in BINDING_DEFINITIONS.items():
        bindings[binding_id] = {
            "enabled": True,
            "source_type": "node",
            "source_id": f"{binding_id}_node",
            "source_label": binding_id,
            "provider_node_id": f"{binding_id}_node",
            "field_bindings": {
                role["id"]: role["id"] for role in definition["roles"]
            },
            "catalog_updated_at": "",
        }
    settings = normalize_training_resource_settings({"bindings": bindings})
    settings["revision"] = 11

    frozen = frozen_training_data_bindings(
        settings, list(BINDING_DEFINITIONS),
    )

    assert frozen["settings_revision"] == 11
    assert set(frozen["bindings"]) == set(BINDING_DEFINITIONS)
    assert {
        item["settings_revision"] for item in frozen["bindings"].values()
    } == {11}
