from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace

import pandas as pd
import pytest

from factor_service.model_research_repository import (
    ModelResearchError,
    _canonical_json,
)
from factor_service.research.dataset_preflight import DatasetPreflightService


def _payload(calendar: pd.DatetimeIndex, split: dict) -> dict:
    return {
        "dataset": {
            "name": "preflight",
            "date_start": calendar[0].date().isoformat(),
            "date_end": calendar[-1].date().isoformat(),
            "data_cutoff": "2026-01-05T15:30:00+08:00",
            "label_horizon_trading_days": 5,
            "factors": [{
                "factor_id": "momentum",
                "factor_version": 1,
                "params_hash": "a" * 64,
                "params": {},
            }],
            "split": split,
        },
    }


def _service(calendar: pd.DatetimeIndex) -> DatasetPreflightService:
    return DatasetPreflightService(
        settings_loader=lambda: SimpleNamespace(),
        calendar_loader=lambda _spec, _settings: calendar,
    )


def test_ratio_preflight_freezes_hash_and_resolves_calendar_segments() -> None:
    calendar = pd.bdate_range("2024-01-02", periods=105)
    result = _service(calendar).validate(_payload(calendar, {
        "mode": "ratio",
        "train": 0.6,
        "valid": 0.2,
        "test": 0.2,
        "embargo_days": 5,
    }))

    assert result["calendar"]["session_count"] == 105
    assert result["calendar"]["trainable_session_count"] == 100
    assert result["calendar"]["trainable_date_end"] == (
        calendar[-6].date().isoformat()
    )
    assert result["segments"]["test"][1] == calendar[-6].date().isoformat()
    assert result["dataset"]["split"]["resolved"]["segments"] == (
        result["segments"]
    )
    assert len(
        result["dataset"]["split"]["resolved"]["calendar"]["fingerprint"]
    ) == 64
    assert result["dataset_hash"] == sha256(
        _canonical_json(result["dataset"]).encode("utf-8")
    ).hexdigest()
    replay = _service(calendar).validate({"dataset": result["dataset"]})
    assert replay["dataset_hash"] == result["dataset_hash"]


def test_preflight_replay_rejects_calendar_drift() -> None:
    calendar = pd.bdate_range("2024-01-02", periods=105)
    result = _service(calendar).validate(_payload(calendar, {
        "mode": "ratio",
        "train": 0.6,
        "valid": 0.2,
        "test": 0.2,
        "embargo_days": 5,
    }))

    drifted = calendar.delete(20)
    with pytest.raises(ModelResearchError, match="交易日历或切分边界已漂移"):
        _service(drifted).validate({"dataset": result["dataset"]})


def test_date_preflight_checks_exact_trading_session_embargo() -> None:
    calendar = pd.bdate_range("2024-01-02", periods=105)
    trainable = calendar[:-5]
    split = {
        "mode": "dates",
        "train": [trainable[0].date().isoformat(), trainable[39].date().isoformat()],
        "validation": [
            trainable[45].date().isoformat(), trainable[64].date().isoformat(),
        ],
        "test": [trainable[70].date().isoformat(), trainable[-1].date().isoformat()],
        "embargo_days": 5,
    }

    result = _service(calendar).validate(_payload(calendar, split))

    assert result["segments"] == {
        "train": split["train"],
        "valid": split["validation"],
        "test": split["test"],
    }
    assert result["calendar"]["embargo_days"] == 5


def test_date_preflight_rejects_gap_that_does_not_match_embargo() -> None:
    calendar = pd.bdate_range("2024-01-02", periods=105)
    trainable = calendar[:-5]
    split = {
        "mode": "dates",
        "train": [trainable[0].date().isoformat(), trainable[39].date().isoformat()],
        "validation": [
            trainable[44].date().isoformat(), trainable[64].date().isoformat(),
        ],
        "test": [trainable[70].date().isoformat(), trainable[-1].date().isoformat()],
        "embargo_days": 5,
    }

    with pytest.raises(ModelResearchError, match="恰好5个交易日"):
        _service(calendar).validate(_payload(calendar, split))


def test_date_preflight_rejects_untrainable_label_tail() -> None:
    calendar = pd.bdate_range("2024-01-02", periods=105)
    trainable = calendar[:-5]
    split = {
        "mode": "dates",
        "train": [trainable[0].date().isoformat(), trainable[39].date().isoformat()],
        "validation": [
            trainable[45].date().isoformat(), trainable[64].date().isoformat(),
        ],
        "test": [
            trainable[70].date().isoformat(), calendar[-1].date().isoformat(),
        ],
        "embargo_days": 5,
    }

    with pytest.raises(ModelResearchError, match="有效交易日"):
        _service(calendar).validate(_payload(calendar, split))
