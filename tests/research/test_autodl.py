from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from factor_service.research import autodl
from factor_service.research.autodl import (
    AutoDLProClient,
    sanitize_snapshot,
    validate_image_name,
    validate_instance_uuid,
)
from factor_service.research.config import Settings
from factor_service.research.job import CancellationToken
from factor_service.research.remote import (
    RemoteResearchExecutor,
    load_remote_nodes,
    node_with_autodl_endpoint,
)
from tests.research.utils import valid_job


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _node_runtime(tmp_path: Path) -> dict:
    return {
        "research": {"execution": {"remote_nodes": [{
            "id": "autodl-pro-01",
            "host": "old.example.test",
            "port": 22022,
            "user": "root",
            "authentication_type": "ssh_private_key",
            "ssh_private_key": (
                "-----BEGIN OPENSSH PRIVATE KEY-----\n"
                "dGVzdA==\n"
                "-----END OPENSSH PRIVATE KEY-----"
            ),
            "work_dir": "/root/alphablocks-research",
            "runner": "direct_python",
            "python_executable": "/root/miniconda3/bin/python",
            "lifecycle_provider": "autodl_pro",
            "instance_uuid": "pro-76576c61fdf1",
            "api_token": "secret-token",
            "auto_start": True,
            "auto_stop": True,
        }]}}
    }


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        clickhouse_host="localhost", clickhouse_port=9000,
        clickhouse_user="default", clickhouse_password="",
        factor_database="ab_factor", model_database="ab_model",
        source_database="starlight", work_root=tmp_path / "work",
        model_artifacts_root=tmp_path / "artifacts", scheduler_enabled=False,
        scheduler_refresh_seconds=60,
    )


def test_autodl_client_uses_fixed_host_and_supplied_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured.update({
            "url": request.full_url,
            "method": request.method,
            "authorization": request.headers["Authorization"],
            "payload": parse_qs(urlparse(request.full_url).query),
            "body": request.data,
            "timeout": timeout,
        })
        return _Response({"code": "Success", "data": "running", "msg": ""})

    monkeypatch.setattr(autodl, "urlopen", fake_urlopen)

    status = AutoDLProClient(
        "pro-76576c61fdf1", "secret-token",
    ).status()

    assert status == "running"
    assert captured == {
        "url": (
            "https://api.autodl.com/api/v1/dev/instance/pro/status"
            "?instance_uuid=pro-76576c61fdf1"
        ),
        "method": "GET",
        "authorization": "secret-token",
        "payload": {"instance_uuid": ["pro-76576c61fdf1"]},
        "body": None,
        "timeout": 30,
    }


def test_autodl_client_rejects_requests_without_token() -> None:
    client = AutoDLProClient("pro-76576c61fdf1", "")

    with pytest.raises(ValueError, match="Token未配置") as exc:
        client.status()

    assert "secret-token" not in str(exc.value)
    assert client.configured() is False


def test_autodl_client_uses_decrypted_database_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.headers["Authorization"]
        return _Response({"code": "Success", "data": "running", "msg": ""})

    monkeypatch.setattr(autodl, "urlopen", fake_urlopen)

    client = AutoDLProClient("pro-76576c61fdf1", "saved-secret-token")

    assert client.configured() is True
    assert client.status() == "running"
    assert captured["authorization"] == "saved-secret-token"


def test_autodl_snapshot_sanitizer_drops_credentials() -> None:
    safe = sanitize_snapshot({
        "proxy_host": "connect.example.test",
        "ssh_port": 39453,
        "root_password": "must-not-return",
        "jupyter_token": "must-not-return-either",
        "usage_info": {"cpu_usage_percent": 12.3, "container_id": "private"},
    })

    assert safe["proxy_host"] == "connect.example.test"
    assert safe["usage_info"] == {"cpu_usage_percent": 12.3}
    assert "must-not-return" not in json.dumps(safe)
    assert "root_password" not in safe


@pytest.mark.parametrize("value", ["", "instance-123", "pro-x", "pro-123;off"])
def test_autodl_rejects_invalid_instance_uuid(value: str) -> None:
    with pytest.raises(ValueError):
        validate_instance_uuid(value)


@pytest.mark.parametrize("value", ["", "\n", "x" * 81])
def test_autodl_rejects_invalid_image_name(value: str) -> None:
    with pytest.raises(ValueError):
        validate_image_name(value)


def test_remote_node_exposes_lifecycle_without_token_value(
    tmp_path: Path,
) -> None:
    node = load_remote_nodes(_node_runtime(tmp_path))[0]

    public = node.public()

    assert public["lifecycle_provider"] == "autodl_pro"
    assert public["instance_uuid"] == "pro-76576c61fdf1"
    assert public["api_token_configured"] is True
    assert public["available"] is True
    assert "secret-token" not in json.dumps(public)
    assert "api_token" not in public


def test_autodl_snapshot_refreshes_dynamic_ssh_endpoint(
    tmp_path: Path,
) -> None:
    node = load_remote_nodes(_node_runtime(tmp_path))[0]

    refreshed = node_with_autodl_endpoint(node, {
        "proxy_host": "connect.example.test", "ssh_port": 39453,
    })

    assert refreshed.host == "connect.example.test"
    assert refreshed.port == 39453
    assert node.host == "old.example.test"


def test_remote_executor_powers_on_waits_for_ssh_and_refreshes_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AUTODL_TOKEN", "secret-token")
    node = load_remote_nodes(_node_runtime(tmp_path))[0]
    calls = []

    class FakeLifecycle:
        def status(self):
            calls.append("status")
            return "stopped"

        def power_on(self):
            calls.append("power_on")

        def wait_for_running(self, **kwargs):
            calls.append("wait_for_running")
            kwargs["checkpoint"]()
            kwargs["on_status"]("starting")
            return "running"

        def snapshot(self):
            calls.append("snapshot")
            return {"proxy_host": "connect.example.test", "ssh_port": 39453}

        def power_off(self):
            calls.append("power_off")

    class FakeTransport:
        def __init__(self, active_node):
            self.node = active_node

        def ssh(self, command, *, timeout, cancellation=None):
            calls.append(("ssh", self.node.host, self.node.port, command))
            return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(
        "factor_service.research.remote.RemoteTransport", FakeTransport,
    )
    executor = RemoteResearchExecutor(_settings(tmp_path), node)
    executor.lifecycle = FakeLifecycle()
    events = []

    executor._prepare_lifecycle(
        cancellation=CancellationToken(),
        progress=lambda stage, percent, details: events.append(stage),
    )

    assert calls[:4] == ["status", "power_on", "wait_for_running", "snapshot"]
    assert calls[-1] == ("ssh", "connect.example.test", 39453, "true")
    assert "remote_powering_on" in events
    assert executor.node.host == "connect.example.test"


def test_remote_executor_stages_dataset_before_starting_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AUTODL_TOKEN", "secret-token")
    executor = RemoteResearchExecutor(
        _settings(tmp_path), load_remote_nodes(_node_runtime(tmp_path))[0],
    )
    monkeypatch.setattr(
        "factor_service.research.remote.DatasetBuilder", lambda _settings: object(),
    )
    calls = []

    class SnapshotStore:
        def get_or_create(self, *_args, **_kwargs):
            calls.append("dataset")
            return SimpleNamespace(reused=False)

    executor.snapshot_store = SnapshotStore()

    def stop_after_lifecycle(**_kwargs):
        calls.append("lifecycle")
        raise RuntimeError("stop after lifecycle ordering check")

    executor._prepare_lifecycle = stop_after_lifecycle
    executor._power_off_after_job = lambda *_args: calls.append("power_off")

    with pytest.raises(RuntimeError, match="ordering check"):
        executor.train(
            valid_job(), tmp_path / "attempt",
            cancellation=CancellationToken(), progress=lambda *_args: None,
        )

    assert calls == ["dataset", "lifecycle", "power_off"]


def test_dataset_failure_never_calls_autodl_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AUTODL_TOKEN", "secret-token")
    executor = RemoteResearchExecutor(
        _settings(tmp_path), load_remote_nodes(_node_runtime(tmp_path))[0],
    )
    monkeypatch.setattr(
        "factor_service.research.remote.DatasetBuilder", lambda _settings: object(),
    )
    calls = []

    class FailingSnapshotStore:
        def get_or_create(self, *_args, **_kwargs):
            calls.append("dataset")
            raise ValueError("dataset generation failed")

    executor.snapshot_store = FailingSnapshotStore()
    executor._prepare_lifecycle = lambda **_kwargs: calls.append("lifecycle")
    executor._power_off_after_job = lambda *_args: calls.append("power_off")

    with pytest.raises(ValueError, match="dataset generation failed"):
        executor.train(
            valid_job(), tmp_path / "attempt",
            cancellation=CancellationToken(), progress=lambda *_args: None,
        )

    assert calls == ["dataset"]


def test_experiment_keeps_autodl_alive_only_for_ready_pending_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AUTODL_TOKEN", "secret-token")
    executor = RemoteResearchExecutor(
        _settings(tmp_path), load_remote_nodes(_node_runtime(tmp_path))[0],
    )
    pending_hash = "b" * 64
    pending = {
        "job_id": "model_job_pending",
        "status": "queued",
        "dataset_hash": pending_hash,
        "config_json": {"execution": {"node_id": executor.node.node_id}},
    }

    class Repository:
        def list_jobs(self, **_kwargs):
            return [pending]

    monkeypatch.setattr(
        "factor_service.model_research_repository.ModelResearchRepository",
        Repository,
    )
    job = valid_job()
    job["config_json"]["experiment"] = {"experiment_id": "experiment_test"}

    assert executor._experiment_has_pending_remote_jobs(job) is False

    snapshot = executor.snapshot_store.artifacts.root / "datasets" / pending_hash
    snapshot.mkdir(parents=True)
    for name in ("dataset.parquet", "dataset_raw.parquet", "dataset_manifest.json"):
        (snapshot / name).write_bytes(b"ready")

    assert executor._experiment_has_pending_remote_jobs(job) is True


def test_remote_executor_still_powers_off_when_progress_is_canceled() -> None:
    calls = []

    class Lifecycle:
        def power_off(self):
            calls.append("power_off")

    executor = object.__new__(RemoteResearchExecutor)
    executor.lifecycle = Lifecycle()
    executor.node = type("Node", (), {
        "auto_stop": True, "node_id": "autodl-pro-01",
    })()

    executor._power_off_after_job(
        {"job_id": "model_job_test", "config_json": {}},
        lambda *_args: (_ for _ in ()).throw(RuntimeError("job canceled")),
    )

    assert calls == ["power_off"]


def test_remote_executor_keeps_instance_alive_for_pending_experiment_jobs() -> None:
    calls = []

    class Lifecycle:
        def power_off(self):
            calls.append("power_off")

    executor = object.__new__(RemoteResearchExecutor)
    executor.lifecycle = Lifecycle()
    executor.node = type("Node", (), {
        "auto_stop": True, "node_id": "autodl-pro-01",
    })()
    executor._experiment_has_pending_remote_jobs = lambda _job: True
    events = []

    executor._power_off_after_job(
        {"job_id": "model_job_test", "config_json": {}},
        lambda stage, percent, details: events.append((stage, percent, details)),
    )

    assert calls == []
    assert events == [(
        "remote_kept_alive", 89,
        {"node_id": "autodl-pro-01", "reason": "experiment_jobs_pending"},
    )]
