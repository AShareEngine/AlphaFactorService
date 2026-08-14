from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from factor_service.api.research import router
from factor_service.research.worker import ResearchWorker


class _Worker(ResearchWorker):
    def __init__(self, *, token: str = "secret") -> None:
        self.settings = SimpleNamespace(worker_token=token)
        self.submitted: list[dict] = []

    def status(self) -> dict:
        return {
            "ok": True,
            "ready": True,
            "busy": False,
            "secret": "only-status-exposes-this",
        }

    def submit(self, payload: dict) -> dict:
        self.submitted.append(payload)
        return {"accepted": True, "job_id": payload["job_id"]}


def _client(worker: ResearchWorker | None) -> TestClient:
    app = FastAPI()
    if worker is not None:
        app.state.research_worker = worker
    app.include_router(router)
    return TestClient(app)


def test_health_and_ready_are_served_by_unified_api() -> None:
    client = _client(_Worker())

    assert client.get("/research/health").status_code == 200
    ready = client.get("/research/ready")

    assert ready.status_code == 200
    assert ready.json() == {
        "ok": True,
        "service": "AlphaFactorServiceResearch",
        "ready": True,
        "busy": False,
    }


def test_status_and_submit_use_same_worker_and_token() -> None:
    worker = _Worker()
    client = _client(worker)

    assert client.get("/research/api/v1/status").status_code == 401
    status = client.get(
        "/research/api/v1/status",
        headers={"Authorization": "Bearer secret"},
    )
    response = client.post(
        "/research/api/v1/jobs",
        headers={"Authorization": "Bearer secret"},
        json={"job_id": "job-1", "lease_token": "lease-1"},
    )

    assert status.status_code == 200
    assert status.json()["secret"] == "only-status-exposes-this"
    assert response.status_code == 202
    assert response.json()["job"]["job_id"] == "job-1"
    assert worker.submitted == [{"job_id": "job-1", "lease_token": "lease-1"}]


def test_research_endpoints_fail_closed_before_startup() -> None:
    client = _client(None)

    response = client.get("/research/ready")

    assert response.status_code == 503
    assert response.json()["error"] == "研究调度器尚未启动"
