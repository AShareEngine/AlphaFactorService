from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from factor_service.api import model_research
from factor_service.schemas import ModelBacktestJobOut


class _Repository:
    def __init__(self) -> None:
        self.jobs = {
            "job-1": {"job_id": "job-1", "status": "queued"},
            "job-done": {"job_id": "job-done", "status": "succeeded"},
        }
        self.inference_payload = None
        self.validation_payload = None

    def list_jobs(self, *, status, limit):
        return list(self.jobs.values())[:limit]

    def create_training_job(self, payload):
        job = {"job_id": "job-created", "status": "queued", "title": payload["title"]}
        self.jobs[job["job_id"]] = job
        return job

    def get_job(self, job_id):
        return dict(self.jobs[job_id])

    def claim_specific_job(self, job_id, *, lease_seconds):
        job = {
            **self.jobs[job_id],
            "status": "leased",
            "lease_owner": "alpha-factor-service",
            "lease_token": "lease-1",
            "attempt_count": 1,
        }
        self.jobs[job_id] = job
        return dict(job)

    def release_dispatch_lease(self, *_args, **_kwargs):
        raise AssertionError("successful dispatch must not release the lease")

    def get_model(self, model_id, version):
        return {
            "model_id": model_id,
            "version": version,
            "dataset_spec": {"factors": [{"factor_id": "mom_20"}]},
            "metrics_json": {"test_days": 80, "rank_ic": 0.04, "ic_ir": 0.6},
            "manifest_json": {},
            "state": "validated",
        }

    def mark_validated(self, model_id, version, backtest_job_id, *, validation):
        self.validation_payload = dict(validation)
        return {"model_id": model_id, "version": version, "state": "validated"}

    def record_validation_result(
        self, model_id, version, backtest_job_id, *, approved, validation,
    ):
        self.validation_payload = {**validation, "approved_by_repository": approved}
        return {"model_id": model_id, "version": version}

    def create_inference_job(self, model_id, version, payload):
        self.inference_payload = dict(payload)
        return {
            "job_id": "infer-done",
            "model_id": model_id,
            "model_version": version,
            "status": "succeeded",
        }


class _Scheduler:
    def __init__(self) -> None:
        self.submitted = []

    def submit(self, job):
        self.submitted.append(dict(job))
        return {"accepted": True, "job_id": job["job_id"]}


def _client(monkeypatch, repository, scheduler) -> TestClient:
    app = FastAPI()
    app.state.research_worker = scheduler
    app.include_router(model_research.router)
    monkeypatch.setattr(model_research, "repository", repository)
    return TestClient(app)


def test_training_job_is_created_and_dispatched_locally(monkeypatch) -> None:
    repository = _Repository()
    scheduler = _Scheduler()
    client = _client(monkeypatch, repository, scheduler)

    created = client.post("/model-research/jobs", json={"title": "smoke"})
    dispatched = client.post("/model-research/jobs/job-1/dispatch", json={})

    assert created.status_code == 201
    assert created.json()["job"]["job_id"] == "job-created"
    assert dispatched.status_code == 202
    assert dispatched.json()["service"]["accepted"] is True
    assert scheduler.submitted[0]["lease_owner"] == "alpha-factor-service"


def test_succeeded_job_dispatch_is_idempotent(monkeypatch) -> None:
    scheduler = _Scheduler()
    client = _client(monkeypatch, _Repository(), scheduler)

    response = client.post("/model-research/jobs/job-done/dispatch", json={})

    assert response.status_code == 200
    assert response.json()["service"]["accepted"] is False
    assert scheduler.submitted == []


def test_default_inference_date_is_rechecked_as_an_explicit_date(monkeypatch) -> None:
    repository = _Repository()
    client = _client(monkeypatch, repository, _Scheduler())
    calls = []

    def availability(_model, *, trade_date="", data_cutoff=""):
        calls.append((trade_date, data_cutoff))
        return {
            "trade_date": "2026-08-13",
            "requested_trade_date_available": True if trade_date else None,
        }

    monkeypatch.setattr(model_research, "_inference_availability", availability)
    response = client.post(
        "/model-research/models/demo/versions/1/inferences",
        json={"data_cutoff": "2026-08-13T16:00:00+08:00"},
    )

    assert response.status_code == 200
    assert calls == [
        ("", "2026-08-13T16:00:00+08:00"),
        ("2026-08-13", "2026-08-13T16:00:00+08:00"),
    ]
    assert repository.inference_payload["trade_date"] == "2026-08-13"


def _negative_backtest() -> ModelBacktestJobOut:
    return ModelBacktestJobOut(
        backtest_job_id="model_backtest_negative",
        model_id="demo",
        model_version=1,
        universe_id="csi500",
        benchmark_code="000905.SH",
        date_preset="3y",
        status="success",
        annual_return=-0.02,
        excess_annual_return=-0.01,
        sharpe_ratio=0.1,
        turnover_rate=0.08,
        max_drawdown=-0.12,
        trading_days=80,
    )


def test_successful_backtest_does_not_auto_validate_negative_excess(monkeypatch) -> None:
    repository = _Repository()
    client = _client(monkeypatch, repository, _Scheduler())
    monkeypatch.setattr(
        model_research.model_repository,
        "get_model_backtest_job",
        lambda _job_id: _negative_backtest(),
    )

    response = client.get(
        "/model-research/model-backtests/model_backtest_negative"
    )

    assert response.status_code == 200
    assert response.json()["backtest"]["validation"]["passed"] is False
    assert repository.validation_payload["approved_by_repository"] is False


def test_model_response_uses_effective_gate_state() -> None:
    model = _Repository().get_model("demo", 1)

    response = model_research._model_response(model, _negative_backtest())

    assert response["registry_state"] == "validated"
    assert response["state"] == "candidate"
    assert response["validation"]["failed_checks"] == ["excess_annual_return"]


def test_failed_gate_requires_reasoned_manual_override(monkeypatch) -> None:
    repository = _Repository()
    client = _client(monkeypatch, repository, _Scheduler())
    monkeypatch.setattr(
        model_research.model_repository,
        "get_model_backtest_job",
        lambda _job_id: _negative_backtest(),
    )
    path = "/model-research/models/demo/versions/1/backtests/model_backtest_negative/validate"

    rejected = client.post(path, json={})
    missing_reason = client.post(path, json={"override": True})
    approved = client.post(
        path,
        json={"override": True, "reason": "仅用于模拟盘观察"},
    )

    assert rejected.status_code == 409
    assert missing_reason.status_code == 400
    assert approved.status_code == 200
    assert repository.validation_payload["manual_override"] is True
    assert repository.validation_payload["override_reason"] == "仅用于模拟盘观察"
