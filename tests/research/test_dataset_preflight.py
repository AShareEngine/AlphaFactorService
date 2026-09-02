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


def test_preflight_can_resolve_calendar_before_features_are_selected() -> None:
    calendar = pd.bdate_range("2020-01-02", periods=800)
    payload = _payload(calendar, {
        "mode": "ratio",
        "train": 0.6,
        "valid": 0.2,
        "test": 0.2,
        "embargo_days": 5,
    })
    payload["dataset"]["factors"] = []
    payload["walk_forward"] = {
        "enabled": True,
        "strategy": "rolling",
        "train_sessions": 504,
        "valid_sessions": 126,
        "test_sessions": 20,
        "step_sessions": 20,
        "embargo_sessions": 5,
        "oos_date_start": "",
        "oos_date_end": "",
    }

    result = _service(calendar).validate(payload)

    assert result["calendar_only"] is True
    assert result["dataset"]["factors"] == []
    assert result["walk_forward"]["prediction_date_start"] == (
        calendar[640].date().isoformat()
    )
    assert result["walk_forward"]["backtest_date_start"] == (
        calendar[641].date().isoformat()
    )


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


def test_walk_forward_preflight_resolves_earliest_oos_from_frozen_calendar() -> None:
    calendar = pd.bdate_range("2020-01-02", periods=800)
    payload = _payload(calendar, {
        "mode": "ratio",
        "train": 0.6,
        "valid": 0.2,
        "test": 0.2,
        "embargo_days": 5,
    })
    payload["walk_forward"] = {
        "enabled": True,
        "strategy": "rolling",
        "train_sessions": 504,
        "valid_sessions": 126,
        "test_sessions": 20,
        "step_sessions": 20,
        "embargo_sessions": 5,
        "oos_date_start": "",
        "oos_date_end": "",
    }

    result = _service(calendar).validate(payload)

    expected_start = calendar[504 + 126 + 10].date().isoformat()
    expected_end = calendar[-6].date().isoformat()
    assert result["segment_session_counts"] == {
        "train": 472,
        "valid": 154,
        "test": 159,
    }
    walk_forward = result["walk_forward"]
    assert walk_forward["earliest_oos_date"] == expected_start
    assert walk_forward["prediction_date_start"] == expected_start
    assert walk_forward["prediction_date_end"] == expected_end
    assert walk_forward["backtest_date_start"] == calendar[641].date().isoformat()
    assert walk_forward["backtest_date_end"] == expected_end
    assert walk_forward["required_history_sessions"] == 640
    assert walk_forward["window_count"] > 0
    assert walk_forward["first_window"]["test"][0] == expected_start
    assert walk_forward["last_window"]["test"][1] == expected_end
    assert len(walk_forward["windows"]) == walk_forward["window_count"]
    first_timeline_window = walk_forward["windows"][0]
    assert first_timeline_window["index"] == 1
    assert first_timeline_window["train"]["sessions"] == 504
    assert first_timeline_window["train_valid_embargo"]["sessions"] == 5
    assert first_timeline_window["valid"]["sessions"] == 126
    assert first_timeline_window["valid_test_embargo"]["sessions"] == 5
    assert first_timeline_window["test"]["sessions"] == 20
    assert first_timeline_window["test"]["date_start"] == expected_start
    assert walk_forward["windows"][-1]["test"]["date_end"] == expected_end
    assert walk_forward["spec"]["oos_date_start"] == expected_start
    assert walk_forward["spec"]["oos_date_start_mode"] == "automatic"


def test_walk_forward_preflight_timeline_preserves_expanding_train_start() -> None:
    calendar = pd.bdate_range("2020-01-02", periods=900)
    payload = _payload(calendar, {
        "mode": "ratio",
        "train": 0.6,
        "valid": 0.2,
        "test": 0.2,
        "embargo_days": 5,
    })
    payload["walk_forward"] = {
        "enabled": True,
        "strategy": "expanding",
        "train_sessions": 504,
        "valid_sessions": 126,
        "test_sessions": 20,
        "step_sessions": 20,
        "embargo_sessions": 5,
        "oos_date_start": "",
        "oos_date_end": "",
    }

    windows = _service(calendar).validate(payload)["walk_forward"]["windows"]

    assert len(windows) > 1
    assert windows[0]["train"]["date_start"] == windows[1]["train"]["date_start"]
    assert windows[0]["train"]["sessions"] == 504
    assert windows[1]["train"]["sessions"] == 524
    assert windows[1]["test"]["date_start"] > windows[0]["test"]["date_end"]


def test_walk_forward_preflight_rejects_manual_start_before_earliest_oos() -> None:
    calendar = pd.bdate_range("2020-01-02", periods=800)
    payload = _payload(calendar, {
        "mode": "ratio",
        "train": 0.6,
        "valid": 0.2,
        "test": 0.2,
        "embargo_days": 5,
    })
    payload["walk_forward"] = {
        "enabled": True,
        "train_sessions": 504,
        "valid_sessions": 126,
        "test_sessions": 20,
        "step_sessions": 20,
        "embargo_sessions": 5,
        "oos_date_start": calendar[639].date().isoformat(),
        "oos_date_end": calendar[-6].date().isoformat(),
    }

    with pytest.raises(ModelResearchError, match="只能向后调整"):
        _service(calendar).validate(payload)
