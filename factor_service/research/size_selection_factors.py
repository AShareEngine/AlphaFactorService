from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def size_selection_factor_payloads() -> Iterator[dict[str, Any]]:
    common_daily = {
        "entity_type": "stock",
        "category": "大小盘选股",
        "group_name": "size_selection",
        "output_type": "number",
        "frequency": "daily",
        "asset_id": "stock",
        "source_node_id": "stock_daily_real",
        "enabled": True,
    }
    common_finance = {
        **common_daily,
        "category": "财务质量",
        "group_name": "size_selection_quality",
        "source_node_id": "fundamentals_pit_real",
    }
    daily_processing = {
        "data_processing": {
            "winsorize": "quantile",
            "standardize": "zscore",
            "neutralize": [],
        },
        "weighting": "equal",
    }
    finance_processing = {
        **daily_processing,
        "_force_entity_asset_source": True,
        "_source_asset": "asset_fundamentals_pit_fundamentals_pit_real",
    }
    definitions = (
        {
            "factor_id": "float_size_continuous",
            "label": "连续流通市值风格",
            "description": (
                "raw_value=-Log(float_market_cap)，数值越高越偏小盘；"
                "使用股票日线实体资产的流通市值，不使用固定市值阈值。"
            ),
            "expression": "-Log($float_market_cap)",
            "params": daily_processing,
            **common_daily,
        },
        {
            "factor_id": "momentum_10_adj",
            "label": "10日后复权动量",
            "description": "后复权收盘价相对10个交易日前的收益率。",
            "expression": "PeriodReturn($close_adj, 10)",
            "params": daily_processing,
            **common_daily,
        },
        {
            "factor_id": "reversal_5_adj",
            "label": "5日后复权反转",
            "description": "5日后复权收益率取负，数值越高表示短期回撤越大。",
            "expression": "-PeriodReturn($close_adj, 5)",
            "params": daily_processing,
            **common_daily,
        },
        {
            "factor_id": "realized_volatility_20",
            "label": "20日实现波动率",
            "description": "过去20个交易日涨跌幅的样本标准差，输入涨跌幅转换为小数。",
            "expression": "Std($pct_chg / 100, 20)",
            "params": daily_processing,
            **common_daily,
        },
        {
            "factor_id": "amount_liquidity_20",
            "label": "20日成交额流动性",
            "description": "过去20个交易日平均成交额的自然对数。",
            "expression": "Log(Mean($amount, 20))",
            "params": daily_processing,
            **common_daily,
        },
        {
            "factor_id": "current_ratio_pit",
            "label": "PIT流动比率",
            "description": "按财报可得时点读取的流动资产/流动负债。",
            "expression": (
                "$total_current_assets / NullIf($total_current_liability, 0)"
            ),
            "params": finance_processing,
            **common_finance,
        },
        {
            "factor_id": "operating_cashflow_to_assets_pit",
            "label": "PIT经营现金流资产比",
            "description": "按财报可得时点读取的经营现金流净额/总资产。",
            "expression": "$net_operate_cash_flow / NullIf($total_assets, 0)",
            "params": finance_processing,
            **common_finance,
        },
        {
            "factor_id": "operating_cashflow_to_profit_pit",
            "label": "PIT现金盈利质量",
            "description": "按财报可得时点读取的经营现金流净额/净利润。",
            "expression": "$net_operate_cash_flow / NullIf($net_profit, 0)",
            "params": finance_processing,
            **common_finance,
        },
        {
            "factor_id": "roe_quality_pit",
            "label": "PIT净资产收益率",
            "description": "按财报可得时点读取的ROE，交由训练截面统一预处理。",
            "expression": "$roe",
            "params": finance_processing,
            **common_finance,
        },
        {
            "factor_id": "revenue_growth_pit",
            "label": "PIT营收同比增长",
            "description": "按财报可得时点读取的营业收入同比增长率。",
            "expression": "$inc_revenue_year_on_year",
            "params": finance_processing,
            **common_finance,
        },
        {
            "factor_id": "profit_growth_pit",
            "label": "PIT净利润同比增长",
            "description": "按财报可得时点读取的净利润同比增长率。",
            "expression": "$inc_net_profit_year_on_year",
            "params": finance_processing,
            **common_finance,
        },
        {
            "factor_id": "eps_quality_pit",
            "label": "PIT每股收益",
            "description": "按财报可得时点读取的每股收益。",
            "expression": "$eps",
            "params": finance_processing,
            **common_finance,
        },
    )
    for definition in definitions:
        yield {
            **definition,
            "required_fields": [],
            "param_schema": {},
            "availability_policy": {
                "field": "available_at",
                "policy": "persisted_timestamp",
            },
        }


__all__ = ["size_selection_factor_payloads"]
