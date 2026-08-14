from hashlib import sha256
from pathlib import Path
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
        self.puts = []
        self.posts = []

    def put(self, url, *, headers, data, timeout):
        self.puts.append((url, headers, data, timeout))
        return _Response()

    def post(self, url, *, headers, json, timeout):
        self.posts.append((url, headers, json, timeout))
        return _Response()


def test_artifact_upload_chunks_and_carries_lease_headers(tmp_path: Path) -> None:
    path = tmp_path / "model.bin"
    path.write_bytes(b"artifact")
    api = AlphaBlocksApi("http://example/api/model-research", "worker-token")
    session = _Session()
    api.session = session  # type: ignore[assignment]

    api.upload("job-1", "lease-1", "bundle", path)

    assert len(session.puts) == 1
    _, chunk_headers, body, _ = session.puts[0]
    assert chunk_headers["X-Lease-Token"] == "lease-1"
    assert chunk_headers["X-Chunk-SHA256"] == sha256(body).hexdigest()
    _, complete_headers, complete_body, _ = session.posts[0]
    assert "X-Worker-Node" not in complete_headers
    assert complete_headers["X-Lease-Token"] == "lease-1"
    assert complete_body["sha256"] == sha256(path.read_bytes()).hexdigest()


def test_artifact_upload_stops_before_first_chunk_when_canceled(tmp_path: Path) -> None:
    path = tmp_path / "model.bin"
    path.write_bytes(b"artifact")
    api = AlphaBlocksApi("http://example/api/model-research", "worker-token")
    session = _Session()
    api.session = session  # type: ignore[assignment]

    with __import__("pytest").raises(RuntimeError, match="stop"):
        api.upload(
            "job-1", "lease-1", "bundle", path,
            checkpoint=lambda: (_ for _ in ()).throw(RuntimeError("stop")),
        )

    assert session.puts == []
    assert session.posts == []


def test_artifact_upload_id_is_stable_across_retries(tmp_path: Path) -> None:
    path = tmp_path / "model.bin"
    path.write_bytes(b"artifact")
    api = AlphaBlocksApi("http://example/api/model-research", "")
    session = _Session()
    api.session = session  # type: ignore[assignment]

    api.upload("job-1", "lease-1", "bundle", path)
    api.upload("job-1", "lease-1", "bundle", path)

    first_upload_id = session.puts[0][0].split("/")[-2]
    second_upload_id = session.puts[1][0].split("/")[-2]
    assert first_upload_id == second_upload_id


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
