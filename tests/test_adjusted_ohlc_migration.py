from scripts.migrate_factor_formulas_to_adjusted_ohlc import rewrite_expression


def test_rewrite_expression_replaces_raw_ohlc_fields() -> None:
    assert rewrite_expression(
        "Sum((($close - $open) / NullIf($high - $low, 0)) * $volume, 6)"
    ) == (
        "Sum((($close_adj - $open_adj) / "
        "NullIf($high_adj - $low_adj, 0)) * $volume, 6)"
    )


def test_rewrite_expression_keeps_reference_prices_on_adjusted_scale() -> None:
    assert rewrite_expression(
        "Greater($high - $pre_close, 0) + Ge($close, $high_limited)"
    ) == (
        "Greater($high_adj - $pre_close_adj, 0) "
        "+ Ge($close_adj, $high_limited_adj)"
    )


def test_rewrite_expression_leaves_non_price_formula_unchanged() -> None:
    expression = "$volume / NullIf(Mean($volume, 20), 0)"
    assert rewrite_expression(expression) == expression
