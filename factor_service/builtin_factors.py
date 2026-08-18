from __future__ import annotations

from factor_service import repository
from factor_service.schemas import FactorCreate, FactorOut


BASE_MARKET_FEATURES: tuple[dict[str, str], ...] = (
    {"factor_id": "base_open", "label": "开盘价", "field": "open"},
    {"factor_id": "base_high", "label": "最高价", "field": "high"},
    {"factor_id": "base_low", "label": "最低价", "field": "low"},
    {"factor_id": "base_close", "label": "收盘价", "field": "close"},
    {"factor_id": "base_pre_close", "label": "昨收价", "field": "pre_close"},
    {"factor_id": "base_volume", "label": "成交量", "field": "volume"},
    {"factor_id": "base_amount", "label": "成交额", "field": "amount"},
    {"factor_id": "base_turnover_rate", "label": "换手率", "field": "turnover_rate"},
    {"factor_id": "base_pct_chg", "label": "当日涨跌幅", "field": "pct_chg"},
)


def ensure_builtin_factor_definitions() -> list[tuple[FactorOut, str]]:
    results: list[tuple[FactorOut, str]] = []
    for item in BASE_MARKET_FEATURES:
        results.append(repository.ensure_factor_definition(
            _base_market_factor(item), update_existing=True,
        ))
    return results


def _base_market_factor(item: dict[str, str]) -> FactorCreate:
    factor_id = item["factor_id"]
    label = item["label"]
    field = item["field"]
    return FactorCreate(
        factor_id=factor_id,
        label=label,
        description=(
            f"基础日行情字段 {field}。训练使用信号日收盘后可得的当日截面值，"
            "按1%/99%分位缩尾并转为截面Z-Score。"
        ),
        entity_type="stock",
        category="基础行情",
        group_name="base_market",
        output_type="number",
        frequency="daily",
        asset_id="stock",
        source_node_id="stock_daily_real",
        params={
            "_builtin": "base_market.v1",
            "_specs": [{
                "spec_id": f"{factor_id}__default",
                "label": f"{label}默认规格",
                "params": {},
                "is_default": True,
                "enabled": True,
                "sync_mode": "on_demand",
                "created_from": "alphablocks_builtin",
            }],
            "data_processing": {
                "winsorize": "quantile",
                "standardize": "zscore",
                "neutralize": [],
            },
            "weighting": "equal",
        },
        availability_policy={
            "field": "available_at",
            "policy": "persisted_timestamp",
        },
        expression=f"${field}",
        enabled=True,
    )


__all__ = ["BASE_MARKET_FEATURES", "ensure_builtin_factor_definitions"]
