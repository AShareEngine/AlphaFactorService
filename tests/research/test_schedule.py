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

    def record_ensemble_inference(self, model_id, version, **payload):
        self.ensemble_inference = (model_id, version, payload)
        return {"job_id": "ensemble-infer-job", "status": "succeeded"}


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


def test_internal_scheduler_rechecks_current_research_gate(monkeypatch) -> None:
    repository = _Repository([{
        "model_id": "stale-validated-model",
        "model_version": 1,
        "enabled": True,
        "state": "validated",
        "run_after_local": "16:30",
        "metrics_json": {
            "test_days": 80,
            "rank_ic": 0.08,
            "ic_ir": 0.8,
            "validation": {"days": 60, "rank_ic": -0.01, "ic_ir": -0.1},
        },
    }])
    monkeypatch.setattr(
        "factor_service.research.schedule.model_repository.latest_model_backtests",
        lambda _identities: {},
    )

    result = run_inference_schedule_tick(
        repository,
        object(),
        now=datetime(2026, 8, 14, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["submitted"] == []
    assert result["skipped"][0]["reason"] == "research_gate_failed"
    assert "validation_rank_ic" in result["skipped"][0]["failed_checks"]
    assert repository.ticks == []


def test_internal_scheduler_materializes_ensemble_without_worker(monkeypatch) -> None:
    repository = _Repository([{
        "model_id": "ensemble-demo",
        "model_version": 2,
        "model_kind": "ensemble",
        "enabled": True,
        "state": "validated",
        "run_after_local": "16:30",
        "prediction_json": {"latest_trade_date": "2026-08-12"},
        "dataset_hash": "a" * 64,
        "manifest_json": {"ensemble": {
            "fingerprint": "b" * 64,
            "sources": [
                {"model_id": "a", "model_version": 1, "weight": 0.5},
                {"model_id": "b", "model_version": 1, "weight": 0.5},
            ],
        }},
    }])
    monkeypatch.setattr(
        "factor_service.research.schedule.model_repository.ensemble_prediction_dates",
        lambda **_kwargs: [datetime(2026, 8, 13).date()],
    )
    monkeypatch.setattr(
        "factor_service.research.schedule.model_repository.materialize_ensemble_predictions",
        lambda **_kwargs: {"row_count": 500, "date_end": "2026-08-13"},
    )

    result = run_inference_schedule_tick(
        repository,
        object(),
        now=datetime(2026, 8, 14, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["submitted"][0]["mode"] == "ensemble_score_fusion"
    assert result["submitted"][0]["dispatched"] is False
    assert repository.ensemble_inference[2]["predictions"]["row_count"] == 500
    assert repository.ticks[-1][2]["trade_date"] == "2026-08-13"
