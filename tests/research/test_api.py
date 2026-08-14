from types import SimpleNamespace

from factor_service.research.api import AlphaBlocksApi
from factor_service.research.worker import ResearchWorker


class _Response:
    ok = True

    @staticmethod
    def json():
        return {"ok": True}


class _Session:
    def __init__(self) -> None:
        self.headers = {}
        self.requests = []

    def request(self, method, url, *, json, timeout):
        self.requests.append((method, url, json, timeout))
        return _Response()


def test_artifact_registration_sends_only_metadata_and_lease() -> None:
    api = AlphaBlocksApi("http://example/api/model-research", "worker-token")
    session = _Session()
    api.session = session  # type: ignore[assignment]

    api.record_artifact(
        "job-1",
        "lease-1",
        kind="bundle",
        file_name="model.bin",
        relative_path="job-1/bundle/model.bin",
        digest="a" * 64,
        size_bytes=8,
        dataset_hash="b" * 64,
    )

    method, url, payload, _timeout = session.requests[0]
    assert method == "POST"
    assert url.endswith("/worker/jobs/job-1/artifacts")
    assert payload == {
        "lease_token": "lease-1",
        "artifact_kind": "bundle",
        "file_name": "model.bin",
        "relative_path": "job-1/bundle/model.bin",
        "sha256": "a" * 64,
        "size_bytes": 8,
        "dataset_hash": "b" * 64,
    }


def test_api_omits_authorization_header_when_token_is_empty() -> None:
    api = AlphaBlocksApi("http://example/api/model-research", "")

    assert "Authorization" not in api.session.headers


def test_api_ignores_desktop_proxy_environment() -> None:
    api = AlphaBlocksApi("http://10.126.126.3:3000/api/model-research", "")

    assert api.session.trust_env is False


def test_scheduler_push_is_idempotent_for_same_active_job() -> None:
    worker = ResearchWorker.__new__(ResearchWorker)
    worker.active_job_id = "job-1"
    worker.last_job_id = "job-1"
    worker.last_job_status = "running"
    worker.last_error = ""
    worker.stopping = False
    worker.recovery_pending = False
    worker._state_lock = __import__("threading").Lock()
    worker._job_thread = SimpleNamespace(is_alive=lambda: True)

    result = worker.submit({"job_id": "job-1", "lease_token": "lease-1"})

    assert result["accepted"] is True
    assert result["duplicate"] is True
    assert "node_id" not in result
