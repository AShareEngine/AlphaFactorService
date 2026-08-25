from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from factor_service.research import remote, remote_settings
from factor_service.research.remote_settings import (
    create_remote_node_setting,
    delete_remote_node_setting,
    list_remote_node_settings,
    update_remote_node_setting,
)


class _MemoryRemoteNodeRepository:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.legacy_imported = False

    def import_legacy_once(self, payloads) -> bool:
        if self.legacy_imported:
            return False
        for payload in payloads:
            self.nodes.setdefault(str(payload["id"]), dict(payload))
        self.legacy_imported = True
        return True

    def legacy_import_required(self) -> bool:
        return not self.legacy_imported

    def list_nodes(self) -> list[dict]:
        return [dict(payload) for payload in self.nodes.values()]

    def create_node(self, payload) -> dict:
        node_id = str(payload["id"])
        if node_id in self.nodes:
            raise ValueError(f"远程训练节点已存在: {node_id}")
        self.nodes[node_id] = dict(payload)
        return dict(payload)

    def update_node(self, node_id: str, payload) -> dict:
        if node_id not in self.nodes:
            raise ValueError(f"远程训练节点未配置: {node_id}")
        self.nodes[node_id] = dict(payload)
        return dict(payload)

    def delete_node(self, node_id: str) -> bool:
        return self.nodes.pop(node_id, None) is not None


@pytest.fixture(autouse=True)
def node_repository(monkeypatch) -> _MemoryRemoteNodeRepository:
    repository = _MemoryRemoteNodeRepository()
    monkeypatch.setattr(
        remote, "get_remote_node_repository", lambda: repository,
    )
    monkeypatch.setattr(
        remote_settings, "get_remote_node_repository", lambda: repository,
    )
    return repository


def _runtime(path: Path) -> None:
    path.write_text(yaml.safe_dump({
        "service": {"port": 8100},
        "clickhouse": {"password": "must-stay-private"},
        "research": {
            "scheduler": {"enabled": True},
            "execution": {"remote_nodes": []},
        },
    }, sort_keys=False), encoding="utf-8")


def _node(**overrides) -> dict:
    return {
        "id": "autodl-gpu-01",
        "name": "AutoDL A100",
        "enabled": True,
        "host": "gpu.example.test",
        "port": 22022,
        "user": "root",
        "password_env": "TEST_AUTODL_PASSWORD",
        "ssh_key": "",
        "known_hosts": "",
        "work_dir": "/root/alphablocks-research",
        "docker_image": "alphafactor-research:latest",
        "gpus": "all",
        "max_runtime_minutes": 240,
        "cleanup_success": True,
        **overrides,
    }


def test_remote_node_settings_crud_uses_postgresql_and_preserves_runtime_secrets(
    tmp_path: Path, monkeypatch,
) -> None:
    config_path = tmp_path / "runtime.local.yaml"
    _runtime(config_path)
    config_path.chmod(0o600)
    monkeypatch.setenv("ALPHA_FACTOR_RUNTIME_CONFIG", str(config_path))
    monkeypatch.setenv("TEST_AUTODL_PASSWORD", "never-return-this")

    created = create_remote_node_setting(_node())
    listed = list_remote_node_settings()
    updated = update_remote_node_setting(
        "autodl-gpu-01", _node(name="AutoDL RTX 4090", gpus="1"),
    )

    assert created["available"] is True
    assert created["password_environment_configured"] is True
    assert "never-return-this" not in repr(created)
    assert listed[0]["password_env"] == "TEST_AUTODL_PASSWORD"
    assert updated["name"] == "AutoDL RTX 4090"
    assert updated["gpus"] == "1"
    stored = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert stored["clickhouse"]["password"] == "must-stay-private"
    assert stored["research"]["scheduler"] == {"enabled": True}
    assert stored["research"]["execution"]["remote_nodes"] == []
    assert "never-return-this" not in config_path.read_text(encoding="utf-8")
    assert oct(os.stat(config_path).st_mode & 0o777) == "0o600"

    deleted = delete_remote_node_setting("autodl-gpu-01")

    assert deleted == {"id": "autodl-gpu-01", "deleted": True}
    assert list_remote_node_settings() == []


def test_remote_node_settings_rejects_invalid_or_duplicate_nodes(
    tmp_path: Path, monkeypatch,
) -> None:
    config_path = tmp_path / "runtime.local.yaml"
    _runtime(config_path)
    monkeypatch.setenv("ALPHA_FACTOR_RUNTIME_CONFIG", str(config_path))

    create_remote_node_setting(_node())

    try:
        create_remote_node_setting(_node())
    except ValueError as exc:
        assert "已存在" in str(exc)
    else:
        raise AssertionError("duplicate node must be rejected")

    try:
        update_remote_node_setting(
            "autodl-gpu-01", _node(host="gpu;shutdown"),
        )
    except ValueError as exc:
        assert "host无效" in str(exc)
    else:
        raise AssertionError("unsafe host must be rejected")


def test_remote_node_settings_persists_autodl_lifecycle_without_token(
    tmp_path: Path, monkeypatch, node_repository,
) -> None:
    config_path = tmp_path / "runtime.local.yaml"
    _runtime(config_path)
    monkeypatch.setenv("ALPHA_FACTOR_RUNTIME_CONFIG", str(config_path))
    monkeypatch.setenv("TEST_AUTODL_PASSWORD", "ssh-secret")
    monkeypatch.setenv("TEST_AUTODL_API_TOKEN", "api-secret")

    created = create_remote_node_setting(_node(
        lifecycle_provider="autodl_pro",
        instance_uuid="pro-76576c61fdf1",
        api_token_env="TEST_AUTODL_API_TOKEN",
        auto_start=True,
        auto_stop=True,
        boot_timeout_minutes=20,
    ))

    assert created["lifecycle_provider"] == "autodl_pro"
    assert created["api_token_env"] == "TEST_AUTODL_API_TOKEN"
    assert created["api_token_environment_configured"] is True
    assert created["auto_start"] is True
    assert created["auto_stop"] is True
    assert created["boot_timeout_minutes"] == 20
    assert "api-secret" not in repr(created)
    stored = node_repository.nodes["autodl-gpu-01"]
    assert stored["api_token_env"] == "TEST_AUTODL_API_TOKEN"
    assert "api-secret" not in repr(stored)


def test_remote_node_settings_saves_api_token_outside_runtime(
    tmp_path: Path, monkeypatch,
) -> None:
    config_path = tmp_path / "runtime.local.yaml"
    token_path = tmp_path / "secrets" / "autodl_api_token"
    _runtime(config_path)
    monkeypatch.setenv("ALPHA_FACTOR_RUNTIME_CONFIG", str(config_path))
    monkeypatch.setenv("ALPHA_AUTODL_API_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("TEST_AUTODL_PASSWORD", "ssh-secret")
    monkeypatch.delenv("ALPHA_AUTODL_API_TOKEN", raising=False)

    created = create_remote_node_setting(_node(
        lifecycle_provider="autodl_pro",
        instance_uuid="pro-76576c61fdf1",
        api_token_env="ALPHA_AUTODL_API_TOKEN",
        api_token="saved-api-token",
        auto_start=True,
        auto_stop=True,
    ))

    assert created["api_token_configured"] is True
    assert created["api_token_source"] == "secure_file"
    assert "saved-api-token" not in repr(created)
    assert token_path.read_text(encoding="utf-8").strip() == "saved-api-token"
    assert os.stat(token_path).st_mode & 0o777 == 0o600
    stored = config_path.read_text(encoding="utf-8")
    assert "saved-api-token" not in stored
    assert "api_token:" not in stored
