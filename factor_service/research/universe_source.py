from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any, Mapping


REGISTERED_MEMBERSHIP_SOURCE_SCHEMA = (
    "alphablocks.registered-membership-source.v1"
)
CONFIGURED_STOCK_POOL_SOURCE_SCHEMA = (
    "alphablocks.configured-stock-pool-source.v1"
)
HASH = re.compile(r"^[0-9a-f]{64}$")


def normalize_universe_source(
    value: Any,
    *,
    allow_empty: bool = True,
) -> dict[str, Any]:
    if value in (None, {}):
        if allow_empty:
            return {}
        raise ValueError("股票池缺少冻结成员来源")
    if not isinstance(value, Mapping):
        raise ValueError("universe_source必须是对象")
    schema = str(value.get("schema_version") or "")
    if schema == CONFIGURED_STOCK_POOL_SOURCE_SCHEMA:
        return _normalize_configured_stock_pool_source(value)
    return normalize_registered_membership_source(
        value, allow_empty=allow_empty,
    )


def _normalize_configured_stock_pool_source(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    allowed = {
        "schema_version", "source_id", "source_kind", "label", "version",
        "pit", "settings_revision", "binding_id", "binding_fingerprint",
        "selector", "benchmark_code", "config_fingerprint",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            "配置股票池来源包含未知字段: " + ", ".join(unknown)
        )
    if str(value.get("source_kind") or "") != "configured_stock_pool":
        raise ValueError("配置股票池source_kind无效")
    if value.get("pit") is not True:
        raise ValueError("配置股票池必须使用PIT历史成员")
    source_id = _required(value.get("source_id"), "source_id")
    revision = value.get("settings_revision")
    version = value.get("version")
    if type(revision) is not int or revision < 1:
        raise ValueError("配置股票池settings_revision无效")
    if type(version) is not int or version != revision:
        raise ValueError("配置股票池version与设置修订不一致")
    binding_id = _required(value.get("binding_id"), "binding_id")
    if binding_id != "index_membership":
        raise ValueError("配置股票池必须引用index_membership能力")
    binding_fingerprint = str(
        value.get("binding_fingerprint") or ""
    ).strip().lower()
    config_fingerprint = str(
        value.get("config_fingerprint") or ""
    ).strip().lower()
    if not HASH.fullmatch(binding_fingerprint):
        raise ValueError("配置股票池binding_fingerprint无效")
    if not HASH.fullmatch(config_fingerprint):
        raise ValueError("配置股票池config_fingerprint无效")
    selector = value.get("selector")
    if not isinstance(selector, Mapping) or set(selector) != {
        "field_role", "operator", "value",
    }:
        raise ValueError("配置股票池selector结构无效")
    if str(selector.get("field_role") or "") != "index_code":
        raise ValueError("配置股票池selector必须使用index_code角色")
    if str(selector.get("operator") or "") != "eq":
        raise ValueError("配置股票池selector只支持eq")
    selector_value = _required(selector.get("value"), "selector.value")
    benchmark_code = _required(
        value.get("benchmark_code"), "benchmark_code",
    )
    return {
        "schema_version": CONFIGURED_STOCK_POOL_SOURCE_SCHEMA,
        "source_id": source_id,
        "source_kind": "configured_stock_pool",
        "label": str(value.get("label") or source_id).strip()[:160],
        "version": revision,
        "pit": True,
        "settings_revision": revision,
        "binding_id": binding_id,
        "binding_fingerprint": binding_fingerprint,
        "selector": {
            "field_role": "index_code",
            "operator": "eq",
            "value": selector_value,
        },
        "benchmark_code": benchmark_code,
        "config_fingerprint": config_fingerprint,
    }


def normalize_registered_membership_source(
    value: Any,
    *,
    allow_empty: bool = True,
) -> dict[str, Any]:
    if value in (None, {}):
        if allow_empty:
            return {}
        raise ValueError("自定义股票池缺少冻结成员来源")
    if not isinstance(value, Mapping):
        raise ValueError("universe_source必须是对象")
    allowed = {
        "schema_version", "source_id", "source_kind", "label", "pit",
        "asset_id", "asset_version", "asset_version_id",
        "asset_source_hash", "membership_shape",
        "binding", "binding_fingerprint",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            "universe_source包含未公开的原始资产字段: " + ", ".join(unknown)
        )
    if str(value.get("schema_version") or "") != REGISTERED_MEMBERSHIP_SOURCE_SCHEMA:
        raise ValueError("universe_source.schema_version不受支持")
    if str(value.get("source_kind") or "") != "entity_asset":
        raise ValueError("自定义股票池只接受已注册entity_asset")
    if value.get("pit") is not True:
        raise ValueError("自定义股票池必须声明PIT历史成员语义")
    source_id = _required(value.get("source_id"), "source_id")
    asset_id = _required(value.get("asset_id"), "asset_id")
    if source_id != asset_id:
        raise ValueError("universe_source.source_id必须等于asset_id")
    try:
        asset_version = int(value.get("asset_version") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("universe_source.asset_version必须是正整数") from exc
    if asset_version <= 0:
        raise ValueError("universe_source.asset_version必须是正整数")
    asset_version_id = _required(
        value.get("asset_version_id"), "asset_version_id",
    )
    asset_source_hash = str(value.get("asset_source_hash") or "").strip().lower()
    if not HASH.fullmatch(asset_source_hash):
        raise ValueError("universe_source.asset_source_hash必须是SHA256")
    membership_shape = str(value.get("membership_shape") or "").strip()
    if membership_shape not in {"daily_snapshot", "interval"}:
        raise ValueError("自定义股票池成员形态只支持daily_snapshot或interval")
    binding = _normalize_binding(value.get("binding"), membership_shape)
    identity = {
        "asset_id": asset_id,
        "asset_version": asset_version,
        "asset_version_id": asset_version_id,
        "asset_source_hash": asset_source_hash,
        "membership_shape": membership_shape,
        "binding": binding,
    }
    expected_fingerprint = sha256(
        _canonical_json(identity).encode("utf-8")
    ).hexdigest()
    actual_fingerprint = str(
        value.get("binding_fingerprint") or ""
    ).strip().lower()
    if actual_fingerprint != expected_fingerprint:
        raise ValueError("universe_source.binding_fingerprint与冻结身份不一致")
    return {
        "schema_version": REGISTERED_MEMBERSHIP_SOURCE_SCHEMA,
        "source_id": source_id,
        "source_kind": "entity_asset",
        "label": str(value.get("label") or source_id).strip()[:160],
        "pit": True,
        **identity,
        "binding_fingerprint": expected_fingerprint,
    }


def _normalize_binding(value: Any, membership_shape: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("universe_source.binding必须是对象")
    allowed = {
        "binding_id", "source_type", "source_id", "provider_node_id",
        "provider_node_version", "provider_node_version_id",
        "provider_node_source_hash",
        "membership_shape", "field_bindings",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            "universe_source.binding包含未知字段: " + ", ".join(unknown)
        )
    if str(value.get("source_type") or "") != "node":
        raise ValueError("自定义股票池绑定只允许Data SDK node")
    provider_node_id = _required(
        value.get("provider_node_id"), "binding.provider_node_id",
    )
    if str(value.get("source_id") or "") != provider_node_id:
        raise ValueError("自定义股票池绑定source_id必须等于provider_node_id")
    try:
        provider_node_version = int(value.get("provider_node_version") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("自定义股票池绑定node version无效") from exc
    if provider_node_version <= 0:
        raise ValueError("自定义股票池绑定node version无效")
    provider_node_version_id = _required(
        value.get("provider_node_version_id"),
        "binding.provider_node_version_id",
    )
    provider_node_source_hash = str(
        value.get("provider_node_source_hash") or ""
    ).strip().lower()
    if not HASH.fullmatch(provider_node_source_hash):
        raise ValueError("自定义股票池绑定node source hash无效")
    if str(value.get("membership_shape") or "") != membership_shape:
        raise ValueError("自定义股票池绑定成员形态不一致")
    raw_fields = value.get("field_bindings")
    if not isinstance(raw_fields, Mapping):
        raise ValueError("自定义股票池绑定缺少field_bindings")
    required = (
        {"trade_date", "instrument"}
        if membership_shape == "daily_snapshot"
        else {"instrument", "in_date", "out_date"}
    )
    if set(raw_fields) != required:
        raise ValueError("自定义股票池绑定字段角色不完整或包含未知角色")
    fields = {key: _required(raw_fields.get(key), f"binding.{key}") for key in sorted(required)}
    return {
        "binding_id": _required(value.get("binding_id"), "binding.binding_id"),
        "source_type": "node",
        "source_id": provider_node_id,
        "provider_node_id": provider_node_id,
        "provider_node_version": provider_node_version,
        "provider_node_version_id": provider_node_version_id,
        "provider_node_source_hash": provider_node_source_hash,
        "membership_shape": membership_shape,
        "field_bindings": fields,
    }


def _required(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"universe_source.{label}不能为空")
    return text


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


__all__ = [
    "CONFIGURED_STOCK_POOL_SOURCE_SCHEMA",
    "REGISTERED_MEMBERSHIP_SOURCE_SCHEMA",
    "normalize_registered_membership_source",
    "normalize_universe_source",
]
