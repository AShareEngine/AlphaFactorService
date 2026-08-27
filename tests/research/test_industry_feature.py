from __future__ import annotations

import pandas as pd
import pytest

from factor_service.research.industry_feature import (
    INDUSTRY_FEATURE_SCHEMA_VERSION,
    SW2021_L1_CATEGORIES,
    append_industry_one_hot_features,
    industry_feature_names,
    normalize_industry_feature,
)
from factor_service.research.training_resource_settings import (
    frozen_training_data_binding,
    normalize_training_resource_settings,
)


def test_industry_contract_freezes_31_categories_and_unknown_bucket() -> None:
    settings = normalize_training_resource_settings({
        "bindings": {"stock_industry_one_hot": {
            "enabled": True,
            "source_type": "node",
            "source_id": "industry_membership_weight_real",
            "source_label": "行业成分与权重",
            "provider_node_id": "industry_membership_weight_real",
            "field_bindings": {
                "trade_date": "trade_date",
                "instrument": "con_code",
                "industry_code": "index_code",
            },
        }},
    })
    settings["revision"] = 1
    contract = normalize_industry_feature(
        {
            "schema_version": INDUSTRY_FEATURE_SCHEMA_VERSION,
            "enabled": True,
            "data_binding": frozen_training_data_binding(settings),
        },
        default_enabled=False,
    )
    names = industry_feature_names(contract)

    assert contract["schema_version"] == INDUSTRY_FEATURE_SCHEMA_VERSION
    assert contract["taxonomy"] == "sw2021_l1"
    assert contract["encoding"] == "one_hot"
    assert contract["point_in_time"] is True
    assert contract["safe_start"] == "2021-12-13"
    assert contract["categories"] == [
        code for code, _label in SW2021_L1_CATEGORIES
    ]
    assert len(contract["categories"]) == 31
    assert len(names) == 32
    assert len(set(names)) == 32
    assert names[-1] == "industry_sw2021_l1__unknown"


def test_industry_one_hot_keeps_rows_and_maps_missing_or_new_codes_to_unknown() -> None:
    trade_date = pd.Timestamp("2024-01-02")
    features = pd.DataFrame([
        {"trade_date": trade_date, "instrument": "A", "factor": 1.0},
        {"trade_date": trade_date, "instrument": "B", "factor": 2.0},
        {"trade_date": trade_date, "instrument": "C", "factor": 3.0},
    ])
    membership = pd.DataFrame([
        {
            "trade_date": trade_date,
            "instrument": "A",
            "industry_entity": "801010.SI",
        },
        {
            "trade_date": trade_date,
            "instrument": "B",
            "industry_entity": "899999.SI",
        },
    ])
    contract = normalize_industry_feature(
        {"enabled": True}, default_enabled=False,
    )

    encoded, details = append_industry_one_hot_features(
        features, membership, contract,
    )

    names = industry_feature_names(contract)
    unknown = names[-1]
    assert encoded[["trade_date", "instrument", "factor"]].equals(features)
    assert encoded[names].sum(axis=1).tolist() == [1.0, 1.0, 1.0]
    assert encoded.loc[0, "industry_sw2021_l1__801010_si"] == 1.0
    assert encoded.loc[0, unknown] == 0.0
    assert encoded.loc[1, unknown] == 1.0
    assert encoded.loc[2, unknown] == 1.0
    assert details == {
        "feature_names": names,
        "mapped_coverage": pytest.approx(1 / 3),
        "unknown_rows": 2,
        "category_count": 31,
    }


def test_industry_one_hot_rejects_duplicate_signal_day_assignment() -> None:
    trade_date = pd.Timestamp("2024-01-02")
    features = pd.DataFrame([{
        "trade_date": trade_date, "instrument": "A", "factor": 1.0,
    }])
    membership = pd.DataFrame([
        {
            "trade_date": trade_date,
            "instrument": "A",
            "industry_entity": "801010.SI",
        },
        {
            "trade_date": trade_date,
            "instrument": "A",
            "industry_entity": "801030.SI",
        },
    ])

    with pytest.raises(ValueError, match="重复股票归属"):
        append_industry_one_hot_features(
            features,
            membership,
            normalize_industry_feature(
                {"enabled": True}, default_enabled=False,
            ),
        )


def test_industry_contract_rejects_mutable_or_ordinal_encodings() -> None:
    with pytest.raises(ValueError, match="固定申万一级One-hot口径"):
        normalize_industry_feature(
            {"enabled": True, "encoding": "integer"},
            default_enabled=False,
        )

    with pytest.raises(ValueError, match="固定申万一级One-hot口径"):
        normalize_industry_feature(
            {"enabled": True, "categories": ["801010.SI"]},
            default_enabled=False,
        )


def test_disabled_industry_contract_does_not_change_feature_shape() -> None:
    features = pd.DataFrame([{
        "trade_date": pd.Timestamp("2024-01-02"),
        "instrument": "A",
        "factor": 1.0,
    }])

    encoded, details = append_industry_one_hot_features(
        features,
        pd.DataFrame(),
        normalize_industry_feature({"enabled": False}, default_enabled=True),
    )

    assert encoded.equals(features)
    assert encoded is not features
    assert details == {"feature_names": [], "mapped_coverage": None}
