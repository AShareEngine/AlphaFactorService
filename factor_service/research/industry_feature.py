from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

import numpy as np
import pandas as pd


LEGACY_INDUSTRY_FEATURE_SCHEMA_VERSION = "alphablocks.stock-industry-one-hot.v1"
INDUSTRY_FEATURE_SCHEMA_VERSION = "alphablocks.stock-industry-one-hot.v2"
INDUSTRY_TAXONOMY = "sw2021_l1"
INDUSTRY_FEATURE_SOURCE = "sw2021_daily_weight_snapshot"
INDUSTRY_FEATURE_SAFE_START = "2021-12-13"
INDUSTRY_UNKNOWN_CATEGORY = "__UNKNOWN__"
INDUSTRY_FEATURE_PREFIX = "industry_sw2021_l1"

# The published Shenwan 2021 level-one taxonomy is deliberately frozen in the
# dataset contract.  Querying a mutable category list while materializing a
# dataset would allow the same dataset hash to acquire a different shape later.
SW2021_L1_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("801010.SI", "农林牧渔"),
    ("801030.SI", "基础化工"),
    ("801040.SI", "钢铁"),
    ("801050.SI", "有色金属"),
    ("801080.SI", "电子"),
    ("801110.SI", "家用电器"),
    ("801120.SI", "食品饮料"),
    ("801130.SI", "纺织服饰"),
    ("801140.SI", "轻工制造"),
    ("801150.SI", "医药生物"),
    ("801160.SI", "公用事业"),
    ("801170.SI", "交通运输"),
    ("801180.SI", "房地产"),
    ("801200.SI", "商贸零售"),
    ("801210.SI", "社会服务"),
    ("801230.SI", "综合"),
    ("801710.SI", "建筑材料"),
    ("801720.SI", "建筑装饰"),
    ("801730.SI", "电力设备"),
    ("801740.SI", "国防军工"),
    ("801750.SI", "计算机"),
    ("801760.SI", "传媒"),
    ("801770.SI", "通信"),
    ("801780.SI", "银行"),
    ("801790.SI", "非银金融"),
    ("801880.SI", "汽车"),
    ("801890.SI", "机械设备"),
    ("801950.SI", "煤炭"),
    ("801960.SI", "石油石化"),
    ("801970.SI", "环保"),
    ("801980.SI", "美容护理"),
)


def normalize_industry_feature(
    source: Mapping[str, Any] | None,
    *,
    default_enabled: bool,
) -> dict[str, Any]:
    """Return the sole supported immutable stock-industry feature contract."""
    raw = dict(source or {})
    allowed = {
        "schema_version", "enabled", "taxonomy", "encoding", "source",
        "safe_start", "categories", "category_labels", "unknown_category",
        "feature_prefix", "point_in_time", "data_binding",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(
            "industry_feature包含未支持字段: " + ", ".join(unknown)
        )
    enabled = raw.get("enabled", default_enabled)
    if not isinstance(enabled, bool):
        raise ValueError("industry_feature.enabled必须是布尔值")
    categories = [code for code, _label in SW2021_L1_CATEGORIES]
    category_labels = {
        code: label for code, label in SW2021_L1_CATEGORIES
    }
    implicit_schema = (
        LEGACY_INDUSTRY_FEATURE_SCHEMA_VERSION
        if enabled and "data_binding" not in raw
        else INDUSTRY_FEATURE_SCHEMA_VERSION
    )
    schema_version = str(raw.get("schema_version") or implicit_schema).strip()
    if schema_version not in {
        INDUSTRY_FEATURE_SCHEMA_VERSION,
        LEGACY_INDUSTRY_FEATURE_SCHEMA_VERSION,
    }:
        raise ValueError("industry_feature.schema_version不受支持")
    expected = {
        "schema_version": schema_version,
        "enabled": enabled,
        "taxonomy": INDUSTRY_TAXONOMY,
        "encoding": "one_hot",
        "source": INDUSTRY_FEATURE_SOURCE,
        "safe_start": INDUSTRY_FEATURE_SAFE_START,
        "categories": categories,
        "category_labels": category_labels,
        "unknown_category": INDUSTRY_UNKNOWN_CATEGORY,
        "feature_prefix": INDUSTRY_FEATURE_PREFIX,
        "point_in_time": True,
    }
    for key in (
        "taxonomy", "encoding", "source", "safe_start", "categories",
        "category_labels", "unknown_category", "feature_prefix",
        "point_in_time",
    ):
        configured = raw.get(key)
        if configured is not None and configured != expected[key]:
            raise ValueError(f"industry_feature.{key}当前只支持固定申万一级One-hot口径")
    if schema_version == INDUSTRY_FEATURE_SCHEMA_VERSION:
        binding_source = raw.get("data_binding")
        if enabled:
            if not isinstance(binding_source, Mapping):
                raise ValueError("启用行业编码特征前必须冻结设置中心的数据绑定")
            from factor_service.research.training_resource_settings import (
                normalize_frozen_training_data_binding,
            )

            expected["data_binding"] = normalize_frozen_training_data_binding(
                binding_source,
            )
        else:
            if binding_source is not None:
                raise ValueError("未启用行业编码特征时data_binding必须为空")
            expected["data_binding"] = None
    elif "data_binding" in raw:
        raise ValueError("旧版行业编码特征不支持data_binding")
    return expected


def industry_feature_names(
    config: Mapping[str, Any] | None,
) -> list[str]:
    normalized = normalize_industry_feature(config, default_enabled=False)
    if not normalized["enabled"]:
        return []
    categories = [
        *normalized["categories"], normalized["unknown_category"],
    ]
    prefix = str(normalized["feature_prefix"])
    return [f"{prefix}__{_feature_token(value)}" for value in categories]


def append_industry_one_hot_features(
    features: pd.DataFrame,
    membership: pd.DataFrame,
    config: Mapping[str, Any] | None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Append a stable one-hot industry block to stock-date observations."""
    normalized = normalize_industry_feature(config, default_enabled=False)
    names = industry_feature_names(normalized)
    if not normalized["enabled"]:
        return features.copy(), {
            "feature_names": [], "mapped_coverage": None,
        }
    required = {"trade_date", "instrument"}
    if not required.issubset(features.columns):
        raise ValueError("行业One-hot特征缺少trade_date或instrument")
    membership_required = {*required, "industry_entity"}
    if not membership_required.issubset(membership.columns):
        raise ValueError("行业One-hot映射缺少industry_entity")

    observed = features.copy()
    observed["trade_date"] = pd.to_datetime(
        observed["trade_date"], errors="coerce",
    )
    if observed["trade_date"].isna().any():
        raise ValueError("行业One-hot特征包含无效trade_date")
    mapping = membership[[
        "trade_date", "instrument", "industry_entity",
    ]].copy()
    mapping["trade_date"] = pd.to_datetime(
        mapping["trade_date"], errors="coerce",
    )
    duplicates = mapping.duplicated(
        ["trade_date", "instrument"], keep=False,
    )
    if duplicates.any():
        raise ValueError("行业One-hot映射存在同日重复股票归属")
    merged = observed.merge(
        mapping, on=["trade_date", "instrument"], how="left",
        validate="many_to_one",
    )
    known = {str(value) for value in normalized["categories"]}
    raw_industry = merged["industry_entity"].astype("string")
    mapped = raw_industry.isin(known)
    merged["_industry_category"] = raw_industry.where(
        mapped, str(normalized["unknown_category"]),
    )
    categories: Sequence[str] = [
        *[str(value) for value in normalized["categories"]],
        str(normalized["unknown_category"]),
    ]
    for category, name in zip(categories, names):
        merged[name] = (
            merged["_industry_category"] == category
        ).astype(np.float64)
    row_sums = merged[names].sum(axis=1).to_numpy(dtype=np.float64)
    if not np.allclose(row_sums, 1.0):
        raise ValueError("行业One-hot特征必须每行恰好命中一个类别")
    coverage = float(mapped.mean()) if len(merged) else 0.0
    merged.drop(
        columns=["industry_entity", "_industry_category"], inplace=True,
    )
    return merged, {
        "feature_names": names,
        "mapped_coverage": coverage,
        "unknown_rows": int((~mapped).sum()),
        "category_count": len(normalized["categories"]),
    }


def _feature_token(value: Any) -> str:
    token = str(value).strip().lower()
    token = re.sub(r"[^a-z0-9]+", "_", token).strip("_")
    return token or "unknown"


__all__ = [
    "INDUSTRY_FEATURE_SAFE_START",
    "INDUSTRY_FEATURE_SCHEMA_VERSION",
    "LEGACY_INDUSTRY_FEATURE_SCHEMA_VERSION",
    "SW2021_L1_CATEGORIES",
    "append_industry_one_hot_features",
    "industry_feature_names",
    "normalize_industry_feature",
]
