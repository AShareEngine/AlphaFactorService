from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any, Mapping

from factor_service.schemas import FactorOut


ENTITY_FIELD_FEATURE_KIND = "entity_field"
_FEATURE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_entity_field_feature(item: Mapping[str, Any] | None) -> bool:
    return str((item or {}).get("feature_kind") or "").strip() == ENTITY_FIELD_FEATURE_KIND


def normalize_entity_field_feature(item: Mapping[str, Any]) -> dict[str, Any]:
    feature_id = str(item.get("factor_id") or item.get("feature_id") or "").strip()
    entity_id = str(item.get("entity_id") or "stock").strip()
    asset_id = str(item.get("asset_id") or "").strip()
    field = str(item.get("field") or item.get("field_name") or "").strip()
    if not _FEATURE_ID_RE.fullmatch(feature_id):
        raise ValueError("实体字段特征缺少安全的feature_id")
    if entity_id != "stock":
        raise ValueError("模型训练的实体字段当前只支持stock")
    if not asset_id or len(asset_id) > 256:
        raise ValueError(f"实体字段特征{feature_id}缺少asset_id")
    if not _FIELD_RE.fullmatch(field):
        raise ValueError(f"实体字段特征{feature_id}的field不是安全标识符")

    identity_contract = {
        "feature_kind": ENTITY_FIELD_FEATURE_KIND,
        "entity_id": entity_id,
        "asset_id": asset_id,
        "field": field,
        "asset_updated_at": str(item.get("asset_updated_at") or "").strip(),
        "provider_node": str(item.get("provider_node") or "").strip(),
    }
    params_hash = sha256(
        json.dumps(
            identity_contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "feature_kind": ENTITY_FIELD_FEATURE_KIND,
        "factor_id": feature_id,
        "factor_version": 1,
        "params_hash": params_hash,
        "params": {},
        "entity_id": entity_id,
        "asset_id": asset_id,
        "field": field,
        "label": str(item.get("label") or field).strip() or field,
        "category": "基础行情",
        "asset_name": str(item.get("asset_name") or asset_id).strip() or asset_id,
        "asset_updated_at": identity_contract["asset_updated_at"],
        "provider_node": identity_contract["provider_node"],
        "data_type": str(item.get("data_type") or "").strip().lower(),
    }


def validate_entity_field_feature_identity(item: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_entity_field_feature(item)
    if str(item.get("params_hash") or "").strip().lower() != normalized["params_hash"]:
        raise ValueError(
            f"实体字段特征{normalized['factor_id']}的目录指纹已经变化"
        )
    return normalized


def virtual_entity_field_factor(item: Mapping[str, Any]) -> FactorOut:
    normalized = validate_entity_field_feature_identity(item)
    field = normalized["field"]
    is_boolean = normalized["data_type"] in {"bool", "boolean"}
    return FactorOut(
        factor_id=normalized["factor_id"],
        label=normalized["label"],
        description=(
            f"股票实体资产 {normalized['asset_name']} 的已接入字段 {field}。"
            "训练时按信号日PIT复合视图读取，不写入因子定义库。"
        ),
        entity_type="stock",
        category="基础行情",
        group_name="entity_asset_fields",
        output_type="boolean" if is_boolean else "number",
        frequency="daily",
        asset_id="stock",
        source_node_id=normalized["provider_node"],
        required_fields=[field],
        params={
            "_entity_field": {
                "asset_id": normalized["asset_id"],
                "field": field,
                "asset_updated_at": normalized["asset_updated_at"],
            },
            "data_processing": {
                "winsorize": "none" if is_boolean else "quantile",
                "standardize": "none" if is_boolean else "zscore",
                "neutralize": [],
            },
            "weighting": "equal",
        },
        availability_policy={
            "field": "available_at",
            "policy": "entity_asset_point_in_time",
        },
        expression=f"${field}",
        enabled=True,
        version=1,
        available_versions=[1],
        definition_hash=normalized["params_hash"],
    )


__all__ = [
    "ENTITY_FIELD_FEATURE_KIND",
    "is_entity_field_feature",
    "normalize_entity_field_feature",
    "validate_entity_field_feature_identity",
    "virtual_entity_field_factor",
]
