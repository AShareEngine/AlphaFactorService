from __future__ import annotations

import json
from pathlib import Path

import pytest

from factor_service.research.config import Settings
from factor_service.research.remote import (
    _load_remote_result,
    load_remote_nodes,
)
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "gpu;touch /tmp/pwned"),
        ("work_dir", "/root/../tmp"),
        ("docker_image", "image;shutdown"),
        ("password_env", "bad-name"),
    ],
)
def test_remote_node_rejects_command_injection_fields(
    tmp_path: Path, field: str, value: str,
) -> None:
    runtime = _runtime(tmp_path)
    runtime["research"]["execution"]["remote_nodes"][0][field] = value
    with pytest.raises(ValueError):
        load_remote_nodes(runtime)


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
