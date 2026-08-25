from __future__ import annotations

from hashlib import sha256
import json
import re
from typing import Any, Mapping

from factor_service.qlib_formula import CompiledFormula, compile_qlib_formula


MAX_CUSTOM_SAMPLE_FILTERS = 10
MAX_SAMPLE_FILTER_WINDOW = 250
MAX_SAMPLE_FILTER_EXPRESSION_LENGTH = 1000
MAX_SAMPLE_FILTER_NAME_LENGTH = 60

# These fields are exposed by the canonical stock_daily_factor_source view.
# Keeping the list explicit prevents a filter expression from reaching arbitrary
# ClickHouse columns even though the Qlib formula parser already blocks raw SQL.
SAMPLE_FILTER_FIELDS: tuple[dict[str, str], ...] = (
    {"name": "open", "label": "开盘价"},
    {"name": "high", "label": "最高价"},
    {"name": "low", "label": "最低价"},
    {"name": "close", "label": "收盘价"},
    {"name": "open_adj", "label": "后复权开盘价"},
    {"name": "high_adj", "label": "后复权最高价"},
    {"name": "low_adj", "label": "后复权最低价"},
    {"name": "close_adj", "label": "后复权收盘价"},
    {"name": "pre_close", "label": "昨收价"},
    {"name": "pre_close_adj", "label": "后复权昨收价"},
    {"name": "volume", "label": "成交量"},
    {"name": "amount", "label": "成交额"},
    {"name": "turnover_rate", "label": "换手率"},
    {"name": "pct_chg", "label": "涨跌幅"},
    {"name": "pe", "label": "市盈率 PE"},
    {"name": "pb", "label": "市净率 PB"},
    {"name": "is_st", "label": "ST 标记"},
    {"name": "is_suspended", "label": "停牌标记"},
    {"name": "is_wd_sec", "label": "退市整理标记"},
    {"name": "is_xr_sec", "label": "除权除息标记"},
    {"name": "is_kcb", "label": "科创板标记"},
    {"name": "is_cyb", "label": "创业板标记"},
    {"name": "is_bjs", "label": "北交所标记"},
    {"name": "high_limited", "label": "涨停价"},
    {"name": "low_limited", "label": "跌停价"},
)
SAMPLE_FILTER_FIELD_NAMES = frozenset(item["name"] for item in SAMPLE_FILTER_FIELDS)
SAMPLE_FILTER_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _normalize_field_bindings(source: Any) -> list[dict[str, str]]:
    if not isinstance(source, list) or not source:
        raise ValueError("股票实体资产可用字段目录不能为空")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(source, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"第{index}个股票实体字段必须是对象")
        field = str(item.get("field") or item.get("name") or "").strip()
        if not SAMPLE_FILTER_FIELD_RE.fullmatch(field):
            raise ValueError(f"第{index}个股票实体字段名称不是安全标识符")
        if field in seen:
            continue
        entity_id = str(item.get("entity_id") or "stock").strip()
        if entity_id != "stock":
            raise ValueError(f"字段{field}不属于股票实体资产")
        asset_id = str(item.get("asset_id") or "").strip()
        if not asset_id or len(asset_id) > 256:
            raise ValueError(f"字段{field}缺少股票实体资产标识")
        seen.add(field)
        normalized.append({
            "field": field,
            "label": str(item.get("label") or field).strip()[:160] or field,
            "data_type": str(item.get("data_type") or "").strip().lower()[:64],
            "entity_id": entity_id,
            "asset_id": asset_id,
            "asset_name": str(item.get("asset_name") or asset_id).strip()[:160]
            or asset_id,
            "asset_updated_at": str(item.get("asset_updated_at") or "").strip()[:80],
            "provider_node": str(item.get("provider_node") or "").strip()[:256],
        })
    if not normalized:
        raise ValueError("股票实体资产没有可用于公式的字段")
    return normalized


def compile_sample_filter_formula(
    expression: str,
    *,
    allowed_fields: set[str] | frozenset[str] | None = None,
) -> CompiledFormula:
    source = str(expression or "").strip()
    if not source:
        raise ValueError("自定义筛选公式不能为空")
    if len(source) > MAX_SAMPLE_FILTER_EXPRESSION_LENGTH:
        raise ValueError(
            f"自定义筛选公式不能超过{MAX_SAMPLE_FILTER_EXPRESSION_LENGTH}个字符"
        )
    compiled = compile_qlib_formula(
        source,
        params={},
        code_column="code",
        date_column="trade_time",
    )
    if not compiled.fields:
        raise ValueError("自定义筛选公式至少需要引用一个行情字段")
    supported_fields = SAMPLE_FILTER_FIELD_NAMES if allowed_fields is None else allowed_fields
    unsupported = sorted(set(compiled.fields) - supported_fields)
    if unsupported:
        if allowed_fields is None:
            raise ValueError(
                "自定义筛选公式包含不支持的字段: " + ", ".join(unsupported)
            )
        raise ValueError(
            "自定义筛选公式引用了股票实体资产中不可用的字段: "
            + ", ".join(unsupported)
        )
    if compiled.max_window > MAX_SAMPLE_FILTER_WINDOW:
        raise ValueError(
            f"自定义筛选公式的最大历史窗口不能超过{MAX_SAMPLE_FILTER_WINDOW}个交易日"
        )
    return compiled


def normalize_custom_sample_filters(source: Any) -> list[dict[str, Any]]:
    if source is None:
        return []
    if not isinstance(source, list):
        raise ValueError("sample_filters.custom_formulas必须是数组")
    if len(source) > MAX_CUSTOM_SAMPLE_FILTERS:
        raise ValueError(f"自定义筛选公式最多允许{MAX_CUSTOM_SAMPLE_FILTERS}项")

    normalized: list[dict[str, Any]] = []
    seen_expressions: set[str] = set()
    for index, item in enumerate(source, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"第{index}个自定义筛选公式必须是对象")
        name = str(item.get("name") or f"自定义公式 {index}").strip()
        if not name:
            name = f"自定义公式 {index}"
        if len(name) > MAX_SAMPLE_FILTER_NAME_LENGTH:
            raise ValueError(
                f"第{index}个自定义筛选公式名称不能超过"
                f"{MAX_SAMPLE_FILTER_NAME_LENGTH}个字符"
            )
        expression = str(item.get("expression") or "").strip()
        raw_bindings = item.get("field_bindings")
        if raw_bindings is None:
            raw_bindings = item.get("available_fields")
        bindings = (
            _normalize_field_bindings(raw_bindings)
            if raw_bindings is not None
            else []
        )
        binding_by_field = {binding["field"]: binding for binding in bindings}
        compiled = compile_sample_filter_formula(
            expression,
            allowed_fields=set(binding_by_field) if bindings else None,
        )
        if expression in seen_expressions:
            raise ValueError(f"自定义筛选公式重复: {name}")
        seen_expressions.add(expression)
        used_bindings = [
            binding_by_field[field]
            for field in compiled.fields
            if field in binding_by_field
        ]
        identity = (
            {"expression": expression, "field_bindings": used_bindings}
            if bindings else expression
        )
        fingerprint = sha256(
            (
                json.dumps(
                    identity,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if bindings else expression
            ).encode("utf-8")
        ).hexdigest()[:16]
        formula = {
            "formula_id": f"sample_filter_{fingerprint}",
            "name": name,
            "expression": expression,
            "required_fields": compiled.fields,
            "max_window": compiled.max_window,
        }
        if bindings:
            formula["field_bindings"] = used_bindings
        normalized.append(formula)
    return normalized


__all__ = [
    "MAX_CUSTOM_SAMPLE_FILTERS",
    "MAX_SAMPLE_FILTER_WINDOW",
    "SAMPLE_FILTER_FIELDS",
    "compile_sample_filter_formula",
    "normalize_custom_sample_filters",
]
