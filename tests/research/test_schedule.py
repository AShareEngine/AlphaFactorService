from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from factor_service.research.schedule import run_inference_schedule_tick


class _Repository:
    def __init__(self, schedules):
        self.schedules = schedules
        self.ticks = []

    def list_inference_schedules(self):
        return self.schedules

    def record_inference_schedule_tick(self, model_id, version, **payload):
        self.ticks.append((model_id, version, payload))


def test_internal_scheduler_skips_models_before_configured_run_time() -> None:
    repository = _Repository([{
        "model_id": "demo",
        "model_version": 1,
        "enabled": True,
        "state": "validated",
        "run_after_local": "16:30",
    }])

    result = run_inference_schedule_tick(
        repository,
        object(),
        now=datetime(2026, 8, 14, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["submitted"] == []
    assert result["skipped"][0]["reason"] == "before_run_time"
    assert repository.ticks == []


def test_internal_scheduler_records_up_to_date_model(monkeypatch) -> None:
    repository = _Repository([{
        "model_id": "demo",
        "model_version": 1,
        "enabled": True,
        "state": "validated",
        "run_after_local": "16:30",
        "prediction_json": {"latest_trade_date": "2026-08-13"},
        "dataset_spec": {"factors": [{"factor_id": "mom_20"}]},
    }])
    monkeypatch.setattr(
        "factor_service.research.schedule.model_repository.model_inference_dates",
        lambda **_kwargs: [],
    )

    result = run_inference_schedule_tick(
        repository,
        object(),
        now=datetime(2026, 8, 14, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["skipped"][0]["reason"] == "up_to_date"
    assert repository.ticks == [("demo", 1, {})]
