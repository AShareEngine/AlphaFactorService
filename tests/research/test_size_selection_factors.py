from __future__ import annotations

from factor_service.qlib_formula import compile_qlib_formula
from factor_service.research.size_selection_factors import (
    size_selection_factor_payloads,
)


def test_size_selection_factor_bundle_compiles_and_uses_expected_assets() -> None:
    payloads = list(size_selection_factor_payloads())
    by_id = {item["factor_id"]: item for item in payloads}

    assert len(by_id) == len(payloads) == 12
    assert by_id["float_size_continuous"]["expression"] == (
        "-Log($float_market_cap)"
    )
    assert by_id["current_ratio_pit"]["source_node_id"] == (
        "fundamentals_pit_real"
    )
    assert by_id["current_ratio_pit"]["params"][
        "_force_entity_asset_source"
    ] is True
    assert by_id["momentum_10_adj"]["params"]["_source_asset"] == (
        "asset_stock_daily_stock_daily_real"
    )
    for payload in payloads:
        compiled = compile_qlib_formula(
            payload["expression"],
            params=payload["params"],
            code_column="code",
            date_column="trade_date",
        )
        assert compiled.fields
        assert all(
            f"${field}" in payload["expression"]
            for field in compiled.fields
        )
