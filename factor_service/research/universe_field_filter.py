from __future__ import annotations

from hashlib import sha256
import json
import math
import re
from typing import Any, Mapping


UNIVERSE_FIELD_FILTER_SCHEMA_VERSION = (
    "alphablocks.universe-entity-field-filter.v1"
)
MAX_UNIVERSE_FIELD_FILTERS = 20
SUPPORTED_OPERATORS = frozenset({
    "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "between",
    "contains", "starts_with", "ends_with", "is_null", "not_null",
})
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,179}$")
HASH = re.compile(r"^[0-9a-f]{16,64}$")


def normalize_universe_field_filters(source: Any) -> list[dict[str, Any]]:
    if source is None:
        return []
    if not isinstance(source, list):
        raise ValueError("universe_field_filters必须是数组")
    if len(source) > MAX_UNIVERSE_FIELD_FILTERS:
        raise ValueError(
            f"实体资产股票池字段过滤最多{MAX_UNIVERSE_FIELD_FILTERS}条"
        )
    return [
        _normalize_filter(item, index=index)
        for index, item in enumerate(source)
    ]


def _normalize_filter(source: Any, *, index: int) -> dict[str, Any]:
    label = f"universe_field_filters[{index}]"
    if not isinstance(source, Mapping):
        raise ValueError(f"{label}必须是对象")
    raw = dict(source)
    allowed = {
        "schema_version", "kind", "entity_id", "asset_id",
        "asset_updated_at", "provider_node", "field", "source_field",
        "data_type", "operator", "value", "missing_policy", "binding",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{label}包含未知字段: " + ", ".join(unknown))
    if str(raw.get("schema_version") or "") != (
        UNIVERSE_FIELD_FILTER_SCHEMA_VERSION
    ):
        raise ValueError(f"{label}.schema_version不受支持")
    if str(raw.get("kind") or "") != "entity_field":
        raise ValueError(f"{label}.kind必须是entity_field")
    if str(raw.get("entity_id") or "") != "stock":
        raise ValueError(f"{label}只支持stock实体")
    asset_id = _stable_id(raw.get("asset_id"), f"{label}.asset_id")
    provider_node = _identifier(
        raw.get("provider_node"), f"{label}.provider_node",
    )
    field = _identifier(raw.get("field"), f"{label}.field")
    source_field = _identifier(
        raw.get("source_field"), f"{label}.source_field",
    )
    asset_updated_at = _text(
        raw.get("asset_updated_at"), f"{label}.asset_updated_at", 80,
    )
    data_type = _text(raw.get("data_type"), f"{label}.data_type", 40).lower()
    operator = str(raw.get("operator") or "").strip().lower()
    if operator not in SUPPORTED_OPERATORS:
        raise ValueError(f"{label}.operator不受支持")
    if str(raw.get("missing_policy") or "") != "exclude":
        raise ValueError(f"{label}.missing_policy只允许exclude")
    has_value = "value" in raw
    if operator in {"is_null", "not_null"}:
        if has_value:
            raise ValueError(f"{label}.{operator}不能携带value")
        value: Any = None
    else:
        if not has_value or raw.get("value") is None:
            raise ValueError(f"{label}.{operator}缺少value")
        value = _normalized_value(
            raw.get("value"), data_type=data_type, operator=operator,
            label=f"{label}.value",
        )
    binding = _normalize_binding(
        raw.get("binding"), provider_node=provider_node,
        source_field=source_field, label=f"{label}.binding",
    )
    return {
        "schema_version": UNIVERSE_FIELD_FILTER_SCHEMA_VERSION,
        "kind": "entity_field",
        "entity_id": "stock",
        "asset_id": asset_id,
        "asset_updated_at": asset_updated_at,
        "provider_node": provider_node,
        "field": field,
        "source_field": source_field,
        "data_type": data_type,
        "operator": operator,
        "missing_policy": "exclude",
        **({"value": value} if has_value else {}),
        "binding": binding,
    }


def _normalize_binding(
    source: Any,
    *,
    provider_node: str,
    source_field: str,
    label: str,
) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        raise ValueError(f"{label}必须是对象")
    raw = dict(source)
    allowed = {
        "source_type", "source_id", "source_label", "provider_node_id",
        "provider_node_version", "provider_node_version_id",
        "provider_node_source_hash", "provider_node_updated_at",
        "field_bindings", "catalog_updated_at", "fingerprint",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{label}包含未知字段: " + ", ".join(unknown))
    if str(raw.get("source_type") or "") != "node":
        raise ValueError(f"{label}.source_type只允许node")
    source_id = _identifier(raw.get("source_id"), f"{label}.source_id")
    provider_node_id = _identifier(
        raw.get("provider_node_id"), f"{label}.provider_node_id",
    )
    if source_id != provider_node or provider_node_id != provider_node:
        raise ValueError(f"{label}的数据节点身份不一致")
    fields = raw.get("field_bindings")
    if not isinstance(fields, Mapping):
        raise ValueError(f"{label}.field_bindings必须是对象")
    if set(fields) != {"trade_date", "instrument", "value"}:
        raise ValueError(
            f"{label}.field_bindings必须且只能包含trade_date、instrument、value"
        )
    normalized_fields = {
        role: _identifier(fields.get(role), f"{label}.field_bindings.{role}")
        for role in ("trade_date", "instrument", "value")
    }
    if normalized_fields["value"] != source_field:
        raise ValueError(f"{label}.field_bindings.value与source_field不一致")
    version = raw.get("provider_node_version")
    if type(version) is not int or version < 1:
        raise ValueError(f"{label}.provider_node_version必须是正整数")
    version_id = _stable_id(
        raw.get("provider_node_version_id"),
        f"{label}.provider_node_version_id",
    )
    source_hash = str(raw.get("provider_node_source_hash") or "").strip().lower()
    if not HASH.fullmatch(source_hash):
        raise ValueError(f"{label}.provider_node_source_hash无效")
    core = {
        "source_type": "node",
        "source_id": source_id,
        "source_label": _text(
            raw.get("source_label"), f"{label}.source_label", 180,
        ),
        "provider_node_id": provider_node_id,
        "provider_node_version": version,
        "provider_node_version_id": version_id,
        "provider_node_source_hash": source_hash,
        "provider_node_updated_at": _text(
            raw.get("provider_node_updated_at"),
            f"{label}.provider_node_updated_at", 80,
        ),
        "field_bindings": normalized_fields,
        "catalog_updated_at": _text(
            raw.get("catalog_updated_at"), f"{label}.catalog_updated_at", 80,
        ),
    }
    fingerprint = sha256(_canonical_json(core).encode("utf-8")).hexdigest()
    if str(raw.get("fingerprint") or "").strip().lower() != fingerprint:
        raise ValueError(f"{label}.fingerprint与冻结绑定不一致")
    return {**core, "fingerprint": fingerprint}


def _normalized_value(
    value: Any, *, data_type: str, operator: str, label: str,
) -> Any:
    if operator in {"in", "not_in", "between"}:
        if not isinstance(value, list):
            raise ValueError(f"{label}必须是数组")
        if operator in {"in", "not_in"} and not value:
            raise ValueError(f"{label}不能为空")
        if operator == "between" and len(value) != 2:
            raise ValueError(f"{label}必须包含两个边界")
        normalized: Any = list(value)
    else:
        normalized = value
    values = normalized if isinstance(normalized, list) else [normalized]
    if "bool" in data_type and any(type(item) is not bool for item in values):
        raise ValueError(f"{label}必须是布尔值")
    if any(isinstance(item, float) and not math.isfinite(item) for item in values):
        raise ValueError(f"{label}必须是有限值")
    try:
        json.dumps(normalized, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是JSON值") from exc
    return normalized


def _identifier(value: Any, label: str) -> str:
    clean = str(value or "").strip()
    if not IDENTIFIER.fullmatch(clean):
        raise ValueError(f"{label}不是合法节点或字段名")
    return clean


def _stable_id(value: Any, label: str) -> str:
    clean = str(value or "").strip()
    if not STABLE_ID.fullmatch(clean):
        raise ValueError(f"{label}不是合法稳定标识")
    return clean


def _text(value: Any, label: str, maximum: int) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > maximum:
        raise ValueError(f"{label}不能为空或超过{maximum}字符")
    return clean


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


__all__ = [
    "MAX_UNIVERSE_FIELD_FILTERS",
    "SUPPORTED_OPERATORS",
    "UNIVERSE_FIELD_FILTER_SCHEMA_VERSION",
    "normalize_universe_field_filters",
]
