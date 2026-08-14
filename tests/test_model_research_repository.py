from __future__ import annotations

import pytest

from factor_service.model_research_repository import (
    ModelResearchError,
    _dataset_spec,
    _model_spec,
    _walk_forward_spec,
)


def _source() -> dict:
    return {
        "name": "smoke",
        "date_start": "2020-01-01",
        "date_end": "2024-01-01",
        "data_cutoff": "2024-01-01T15:00:00+08:00",
        "factors": [{
            "factor_id": "mom_20",
            "factor_version": 2,
            "params_hash": "a" * 64,
            "params": {"window": 20},
        }],
    }


def test_dataset_contract_locks_versions_and_lookahead_guards() -> None:
    spec = _dataset_spec(_source())

    assert spec["universe_id"] == "csi500"
    assert spec["factors"][0]["factor_version"] == 2
    assert spec["split"]["embargo_days"] == 5
    assert spec["availability"]["event_available_at_lte_signal_close"] is True
    assert spec["availability"]["source_available_at_lte_data_cutoff"] is True
    assert spec["materialization"] == {
        "mode": "on_demand", "format": "parquet", "persist_factor_values": False,
    }


def test_dataset_contract_rejects_unlocked_factor() -> None:
    source = _source()
    del source["factors"][0]["params_hash"]

    with pytest.raises(ModelResearchError, match="params_hash"):
        _dataset_spec(source)


def test_model_contract_uses_whitelist_and_deterministic_seed() -> None:
    spec = _model_spec({"params": {"learning_rate": 0.03, "unsafe": "ignored"}})

    assert spec["params"]["learning_rate"] == 0.03
    assert spec["params"]["seed"] == 42
    assert "unsafe" not in spec["params"]


def test_all_supported_models_have_distinct_training_implementations() -> None:
    expected = {
        "lightgbm": "qlib.contrib.model.gbdt.LGBModel",
        "xgboost": "qlib.contrib.model.xgboost.XGBModel",
        "catboost": "qlib.contrib.model.catboost_model.CatBoostModel",
        "mlp": "factor_service.research.models.QlibTorchMLPModel",
        "lstm": "factor_service.research.models.QlibTorchLSTMModel",
        "transformer_lstm": "factor_service.research.models.QlibTorchTransformerLSTMModel",
    }

    for kind, implementation in expected.items():
        spec = _model_spec({"kind": kind, "params": {}})
        assert spec["qlib_model"] == implementation
        assert spec["params"]["seed"] == 42


def test_mlp_contract_freezes_explicit_hidden_layer_widths() -> None:
    spec = _model_spec({
        "kind": "mlp",
        "params": {"hidden_layers": [64, 128, 256]},
    })

    assert spec["params"]["hidden_layers"] == [64, 128, 256]
    assert "hidden_size" not in spec["params"]
    assert "layer_count" not in spec["params"]


def test_mlp_contract_rejects_invalid_hidden_layer_widths() -> None:
    with pytest.raises(ModelResearchError, match="hidden_layers"):
        _model_spec({"kind": "mlp", "params": {"hidden_layers": [64, 5000]}})


def test_lstm_contract_freezes_sequence_architecture() -> None:
    spec = _model_spec({
        "kind": "lstm",
        "params": {"lookback_window": 40, "hidden_size": 96, "num_layers": 3},
    })

    assert spec["params"]["lookback_window"] == 40
    assert spec["params"]["hidden_size"] == 96
    assert spec["params"]["num_layers"] == 3
    assert spec["params"]["learning_rate"] == 0.001


def test_transformer_lstm_contract_freezes_both_encoders() -> None:
    spec = _model_spec({
        "kind": "transformer_lstm",
        "params": {
            "lookback_window": 40, "d_model": 48, "nhead": 4,
            "transformer_layers": 2, "lstm_hidden_size": 96,
        },
    })

    assert spec["params"]["lookback_window"] == 40
    assert spec["params"]["d_model"] == 48
    assert spec["params"]["nhead"] == 4
    assert spec["params"]["lstm_hidden_size"] == 96


def test_transformer_lstm_contract_rejects_incompatible_heads() -> None:
    with pytest.raises(ModelResearchError, match="d_model.*nhead"):
        _model_spec({
            "kind": "transformer_lstm", "params": {"d_model": 30, "nhead": 8},
        })


def test_walk_forward_contract_has_strict_independent_test_defaults() -> None:
    spec = _walk_forward_spec({"enabled": True})

    assert spec["strategy"] == "rolling"
    assert spec["valid_months"] == 6
    assert spec["test_months"] == 12
    assert spec["step_months"] == 12
    assert spec["embargo_days"] == 5


def test_walk_forward_contract_rejects_overlapping_test_windows() -> None:
    with pytest.raises(ModelResearchError, match="样本外预测重叠"):
        _walk_forward_spec({"enabled": True, "test_months": 12, "step_months": 6})
