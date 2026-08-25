from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from factor_service.research.config import Settings
from factor_service.research.remote import (
    RemoteTransport,
    _effective_remote_thread_count,
    _load_remote_result,
    _requested_job_threads,
    _remote_result_is_complete,
    _source_fingerprint,
    load_remote_nodes,
)
from factor_service.research.remote_runner import _scoped_path
from factor_service.research.trainer import QlibTrainer


def _runtime(tmp_path: Path) -> dict:
    return {
        "research": {
            "execution": {
                "remote_nodes": [{
                    "id": "autodl-gpu-01",
                    "name": "AutoDL GPU",
                    "host": "gpu.example.test",
                    "port": 22022,
                    "user": "root",
                    "password_env": "TEST_AUTODL_PASSWORD",
                    "work_dir": "/root/alphablocks-research",
                    "docker_image": "alphafactor-research:latest",
                    "gpus": "all",
                    "known_hosts": str(tmp_path / "known_hosts"),
                }],
            },
        },
    }


def test_remote_node_public_contract_does_not_expose_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AUTODL_PASSWORD", "private-value")
    node = load_remote_nodes(_runtime(tmp_path))[0]

    public = node.public()

    assert public["id"] == "autodl-gpu-01"
    assert public["available"] is True
    assert public["credential_type"] == "password_env"
    assert "password_env" not in public
    assert "private-value" not in json.dumps(public)


def test_remote_node_supports_autodl_direct_python_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_AUTODL_PASSWORD", "private-value")
    runtime = _runtime(tmp_path)
    runtime["research"]["execution"]["remote_nodes"][0].update({
        "runner": "direct_python",
        "python_executable": "/root/miniconda3/bin/python",
        "work_dir": "/root/autodl-tmp/alphablocks-research",
    })

    node = load_remote_nodes(runtime)[0]

    assert node.runner == "direct_python"
    assert node.python_executable == "/root/miniconda3/bin/python"
    assert node.public()["runner"] == "direct_python"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "gpu;touch /tmp/pwned"),
        ("work_dir", "/root/../tmp"),
        ("docker_image", "image;shutdown"),
        ("password_env", "bad-name"),
        ("runner", "direct;shutdown"),
    ],
)
def test_remote_node_rejects_command_injection_fields(
    tmp_path: Path, field: str, value: str,
) -> None:
    runtime = _runtime(tmp_path)
    runtime["research"]["execution"]["remote_nodes"][0][field] = value
    with pytest.raises(ValueError):
        load_remote_nodes(runtime)


def test_direct_python_runner_rejects_relative_python_path(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime["research"]["execution"]["remote_nodes"][0].update({
        "runner": "direct_python",
        "python_executable": "python",
    })

    with pytest.raises(ValueError, match="安全绝对路径"):
        load_remote_nodes(runtime)


def test_remote_transport_ssh_includes_target_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("test-key", encoding="utf-8")
    runtime = _runtime(tmp_path)
    node_config = runtime["research"]["execution"]["remote_nodes"][0]
    node_config.pop("password_env")
    node_config["ssh_key"] = str(key_path)
    transport = RemoteTransport(load_remote_nodes(runtime)[0])
    captured: list[str] = []

    def fake_run(args, *, timeout, cancellation):
        captured.extend(args)
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(transport, "_run", fake_run)

    transport.ssh("printf ok", timeout=30)

    assert captured.count("root@gpu.example.test") == 1
    assert captured[-1] == "printf ok"


def test_remote_transport_rsync_is_compatible_with_macos_openrsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("test-key", encoding="utf-8")
    source = tmp_path / "dataset.parquet"
    source.write_text("test-data", encoding="utf-8")
    runtime = _runtime(tmp_path)
    node_config = runtime["research"]["execution"]["remote_nodes"][0]
    node_config.pop("password_env")
    node_config["ssh_key"] = str(key_path)
    transport = RemoteTransport(load_remote_nodes(runtime)[0])
    captured: list[str] = []

    def fake_run(args, *, timeout, cancellation):
        captured.extend(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(transport, "_run", fake_run)

    transport.push(source, "/root/alphablocks-research/dataset.parquet")

    assert captured[0] == "rsync"
    assert "-z" not in captured
    assert "-az" not in captured
    assert "--protect-args" not in captured
    assert captured[-2] == str(source)
    assert captured[-1] == (
        "root@gpu.example.test:/root/alphablocks-research/dataset.parquet"
    )


def test_remote_resource_planner_uses_large_node_without_oversubscription() -> None:
    assert _effective_remote_thread_count(96, 4) == 32
    assert _effective_remote_thread_count(8, 4) == 4
    assert _effective_remote_thread_count(6, 16) == 6
    assert _requested_job_threads({
        "config_json": {"model": {
            "kind": "stacking",
            "params": {},
            "base_models": [
                {"kind": "lstm", "params": {"num_threads": 4}},
                {"kind": "gru", "params": {"num_threads": 12}},
            ],
        }},
    }) == 12


def test_source_fingerprint_changes_with_training_code(tmp_path: Path) -> None:
    source = tmp_path / "factor_service"
    source.mkdir()
    module = source / "trainer.py"
    module.write_text("VERSION = 1\n", encoding="utf-8")
    first = _source_fingerprint(source)

    module.write_text("VERSION = 2\n", encoding="utf-8")

    assert len(first) == 24
    assert _source_fingerprint(source) != first


def test_nonzero_remote_cleanup_exit_accepts_atomic_packaged_result() -> None:
    class _Transport:
        @staticmethod
        def ssh(command, *, timeout, cancellation):
            assert "test -s" in command
            assert "remote_packaged" in command
            return subprocess.CompletedProcess([], 0, "", "")

    assert _remote_result_is_complete(
        _Transport(), "/root/run", cancellation=None,
    ) is True


def test_remote_result_rejects_path_traversal(tmp_path: Path) -> None:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir()
    artifacts.mkdir()
    result = work / "remote_result.json"
    result.write_text(json.dumps({
        "schema_version": "alphablocks.remote-training-result.v1",
        "result": {},
        "artifacts": [{"kind": "bundle", "scope": "work", "path": "../escape"}],
        "predictions": {"scope": "work", "path": "predictions.parquet"},
    }), encoding="utf-8")

    with pytest.raises(RuntimeError, match="路径无效"):
        _load_remote_result(result, work, artifacts)


def test_remote_result_scope_preserves_shared_dataset_symlink(tmp_path: Path) -> None:
    work = tmp_path / "run" / "work"
    artifacts = tmp_path / "run" / "artifacts"
    cache = tmp_path / "cache" / "dataset-hash"
    work.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    cache.mkdir(parents=True)
    (cache / "dataset.parquet").write_bytes(b"parquet")
    (artifacts / "datasets").mkdir()
    (artifacts / "datasets" / "dataset-hash").symlink_to(
        cache, target_is_directory=True,
    )

    scoped = _scoped_path(
        artifacts / "datasets" / "dataset-hash" / "dataset.parquet",
        work,
        artifacts,
    )

    assert scoped == {
        "scope": "artifact_root",
        "path": "datasets/dataset-hash/dataset.parquet",
    }


def test_qlib_trainer_does_not_connect_to_clickhouse_during_init(tmp_path: Path) -> None:
    settings = Settings(
        clickhouse_host="unreachable.invalid",
        clickhouse_port=1,
        clickhouse_user="none",
        clickhouse_password="",
        factor_database="ab_factor",
        model_database="ab_model",
        source_database="starlight",
        work_root=tmp_path / "work",
        model_artifacts_root=tmp_path / "artifacts",
        scheduler_enabled=False,
        scheduler_refresh_seconds=60,
    )

    trainer = QlibTrainer(settings)

    assert trainer.dataset_builder is None
