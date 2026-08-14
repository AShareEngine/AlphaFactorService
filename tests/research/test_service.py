from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from factor_service.research.service import WorkerHttpService


class _Worker:
    stopping = False

    def __init__(self) -> None:
        self.submitted = []

    def status(self):
        return {
            "ok": True,
            "ready": True,
            "busy": False,
            "secret": "not-exposed-by-ready",
        }

    def submit(self, payload):
        self.submitted.append(payload)
        return {"accepted": True, "job_id": payload["job_id"]}


@pytest.fixture()
def service():
    worker = _Worker()
    settings = SimpleNamespace(service_host="127.0.0.1", service_port=0, worker_token="secret")
    instance = WorkerHttpService(worker, settings)
    port = instance.server.server_address[1]
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield worker, f"http://127.0.0.1:{port}"
    finally:
        worker.stopping = True
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
            pass
        thread.join(timeout=2)
        instance.close()


def _json(url: str, *, method: str = "GET", token: str = "", payload=None):
    headers = {"Accept": "application/json"}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    with urlopen(Request(url, data=body, headers=headers, method=method), timeout=2) as response:
        return response.status, json.loads(response.read())


def test_health_and_ready_are_public_but_status_requires_token(service) -> None:
    _, base = service
    assert _json(f"{base}/health")[0] == 200
    _, ready = _json(f"{base}/ready")
    assert ready["ready"] is True
    assert "secret" not in ready
    with pytest.raises(HTTPError) as error:
        _json(f"{base}/api/v1/status")
    assert error.value.code == 401
    status = _json(f"{base}/api/v1/status", token="secret")[1]
    assert status["ready"] is True
    assert "node_id" not in status


def test_push_endpoint_accepts_job_asynchronously(service) -> None:
    worker, base = service
    status, payload = _json(
        f"{base}/api/v1/jobs", method="POST", token="secret",
        payload={"job_id": "job-1", "lease_token": "lease-1"},
    )
    assert status == 202
    assert payload["job"]["job_id"] == "job-1"
    assert worker.submitted == [{"job_id": "job-1", "lease_token": "lease-1"}]
