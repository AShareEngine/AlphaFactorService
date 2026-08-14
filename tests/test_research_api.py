from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from factor_service.api.research import router
from factor_service.research.worker import ResearchWorker


class _Worker(ResearchWorker):
    def __init__(self) -> None:
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


def test_status_uses_embedded_scheduler_without_worker_auth() -> None:
    worker = _Worker()
    client = _client(worker)

    status = client.get("/research/status")

    assert status.status_code == 200
    assert status.json()["secret"] == "only-status-exposes-this"
    assert client.post("/research/api/v1/jobs", json={"job_id": "job-1"}).status_code == 404
    assert worker.submitted == []


def test_research_endpoints_fail_closed_before_startup() -> None:
    client = _client(None)

    response = client.get("/research/ready")

    assert response.status_code == 503
    assert response.json()["error"] == "研究调度器尚未启动"
