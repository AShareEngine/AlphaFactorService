from __future__ import annotations

from types import SimpleNamespace

from factor_service.api import factors as factors_api
from factor_service.schemas import FactorValueSyncStatesRequest


def test_parameter_identities_freeze_hash_without_querying_factor_values(monkeypatch) -> None:
    factor = SimpleNamespace(
        factor_id="mean_amount", version=3, asset_id="stock",
    )
    monkeypatch.setattr(
        factors_api.repository, "get_factor",
        lambda factor_id, version: factor,
    )
    monkeypatch.setattr(
        factors_api, "factor_params_hash",
        lambda item, params: "frozen-params-hash",
    )
    monkeypatch.setattr(
        factors_api.repository, "coverage",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not read factor values")),
    )

    result = factors_api.parameter_identities(FactorValueSyncStatesRequest(items=[{
        "factor_id": "mean_amount",
        "factor_version": 3,
        "entity_type": "stock",
        "params": {"window": 20},
    }]))

    assert result[0].factor_id == "mean_amount"
    assert result[0].factor_version == 3
    assert result[0].params_hash == "frozen-params-hash"
