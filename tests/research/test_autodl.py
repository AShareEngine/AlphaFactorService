from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, urlparse

import pytest

from factor_service.research import autodl
from factor_service.research.autodl import (
    AUTODL_TOKEN_FILE_ENV,
    DEFAULT_AUTODL_TOKEN_ENV,
    AutoDLProClient,
    api_token_status,
    save_api_token,
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
    key = tmp_path / "id_ed25519"
    key.write_text("test", encoding="utf-8")
    return {
        "research": {"execution": {"remote_nodes": [{
            "id": "autodl-pro-01",
            "host": "old.example.test",
            "port": 22022,
            "user": "root",
            "ssh_key": str(key),
            "work_dir": "/root/alphablocks-research",
            "runner": "direct_python",
            "python_executable": "/root/miniconda3/bin/python",
            "lifecycle_provider": "autodl_pro",
            "instance_uuid": "pro-76576c61fdf1",
            "api_token_env": "TEST_AUTODL_TOKEN",
            "auto_start": True,
            "auto_stop": True,
        }]}}
    }


def test_autodl_client_uses_fixed_host_and_token_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AUTODL_TOKEN", "secret-token")
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
        "pro-76576c61fdf1", "TEST_AUTODL_TOKEN",
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


def test_autodl_client_never_accepts_or_returns_inline_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_AUTODL_TOKEN", raising=False)
    client = AutoDLProClient("pro-76576c61fdf1", "TEST_AUTODL_TOKEN")

    with pytest.raises(ValueError, match="Token未配置") as exc:
        client.status()

    assert "secret-token" not in str(exc.value)
    assert "token" not in client.__dict__.keys()


def test_autodl_client_reads_secure_token_file_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "secrets" / "autodl_api_token"
    monkeypatch.setenv(AUTODL_TOKEN_FILE_ENV, str(token_file))
    monkeypatch.delenv(DEFAULT_AUTODL_TOKEN_ENV, raising=False)
    save_api_token("saved-secret-token")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.headers["Authorization"]
        return _Response({"code": "Success", "data": "running", "msg": ""})

    monkeypatch.setattr(autodl, "urlopen", fake_urlopen)

    client = AutoDLProClient("pro-76576c61fdf1", DEFAULT_AUTODL_TOKEN_ENV)

    assert client.configured() is True
    assert client.status() == "running"
    assert captured["authorization"] == "saved-secret-token"
    assert api_token_status()["source"] == "secure_file"
    assert os.stat(token_file).st_mode & 0o777 == 0o600


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AUTODL_TOKEN", "secret-token")
    node = load_remote_nodes(_node_runtime(tmp_path))[0]

    public = node.public()

    assert public["lifecycle_provider"] == "autodl_pro"
    assert public["instance_uuid"] == "pro-76576c61fdf1"
    assert public["api_token_environment_configured"] is True
    assert public["available"] is True
    assert "secret-token" not in json.dumps(public)
    assert "api_token_env" not in public


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
    settings = Settings(
        clickhouse_host="localhost", clickhouse_port=9000,
        clickhouse_user="default", clickhouse_password="",
        factor_database="ab_factor", model_database="ab_model",
        source_database="starlight", work_root=tmp_path / "work",
        model_artifacts_root=tmp_path / "artifacts", scheduler_enabled=False,
        scheduler_refresh_seconds=60,
    )
    executor = RemoteResearchExecutor(settings, node)
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
        lambda *_args: (_ for _ in ()).throw(RuntimeError("job canceled")),
    )

    assert calls == ["power_off"]
