from __future__ import annotations

from factor_service import builtin_factors


def test_builtin_base_market_features_are_real_frozen_factor_definitions(
    monkeypatch,
) -> None:
    captured = []

    def ensure(payload, *, update_existing):
        captured.append((payload, update_existing))
        return payload, "created"

    monkeypatch.setattr(builtin_factors.repository, "ensure_factor_definition", ensure)

    results = builtin_factors.ensure_builtin_factor_definitions()

    assert len(results) == len(builtin_factors.BASE_MARKET_FEATURES) == 9
    assert len({payload.factor_id for payload, _ in captured}) == 9
    assert all(payload.category == "基础行情" for payload, _ in captured)
    assert all(payload.group_name == "base_market" for payload, _ in captured)
    assert all(payload.expression.startswith("$") for payload, _ in captured)
    assert all(update_existing is True for _, update_existing in captured)
    assert all(
        payload.params["data_processing"] == {
            "winsorize": "quantile",
            "standardize": "zscore",
            "neutralize": [],
        }
        for payload, _ in captured
    )
