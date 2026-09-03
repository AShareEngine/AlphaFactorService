from __future__ import annotations

import pytest

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
        self.secrets: dict[str, dict] = {}

    def list_nodes(self) -> list[dict]:
        return [
            {**payload, **self.secrets.get(node_id, {})}
            for node_id, payload in self.nodes.items()
        ]

    def create_node(self, payload, secrets) -> dict:
        node_id = str(payload["id"])
        if node_id in self.nodes:
            raise ValueError(f"远程训练节点已存在: {node_id}")
        self.nodes[node_id] = dict(payload)
        self.secrets[node_id] = {
            key: value for key, value in dict(secrets).items() if value
        }
        return {**payload, **self.secrets[node_id]}

    def update_node(self, node_id: str, payload, secrets) -> dict:
        if node_id not in self.nodes:
            raise ValueError(f"远程训练节点未配置: {node_id}")
        self.nodes[node_id] = dict(payload)
        self.secrets[node_id] = {
            key: value for key, value in dict(secrets).items() if value
        }
        return {**payload, **self.secrets[node_id]}

    def delete_node(self, node_id: str) -> bool:
        self.secrets.pop(node_id, None)
        return self.nodes.pop(node_id, None) is not None


@pytest.fixture(autouse=True)
def node_repository(monkeypatch) -> _MemoryRemoteNodeRepository:
    repository = _MemoryRemoteNodeRepository()
    monkeypatch.setattr(remote, "get_remote_node_repository", lambda: repository)
    monkeypatch.setattr(
        remote_settings, "get_remote_node_repository", lambda: repository,
    )
    return repository


def _node(**overrides) -> dict:
    return {
        "id": "autodl-gpu-01",
        "name": "AutoDL A100",
        "enabled": True,
        "host": "gpu.example.test",
        "port": 22022,
        "user": "root",
        "authentication_type": "password",
        "ssh_password": "never-return-this",
        "known_hosts": "",
        "work_dir": "/root/alphablocks-research",
        "runner": "direct_python",
        "python_executable": "/root/miniconda3/bin/python",
        "docker_image": "alphafactor-research:latest",
        "compute_type": "gpu",
        "gpus": "all",
        "max_runtime_minutes": 240,
        "cleanup_success": True,
        "lifecycle_provider": "manual",
        **overrides,
    }


def test_remote_node_settings_crud_stores_secrets_outside_config_json(
    node_repository,
) -> None:
    created = create_remote_node_setting(_node())
    listed = list_remote_node_settings()
    updated = update_remote_node_setting(
        "autodl-gpu-01",
        _node(name="AutoDL RTX 4090", gpus="1", ssh_password=""),
    )

    assert created["available"] is True
    assert created["compute_type"] == "gpu"
    assert created["ssh_password_configured"] is True
    assert created["authentication_hint"] == "PostgreSQL加密SSH密码"
    assert listed[0]["credential_type"] == "password"
    assert updated["name"] == "AutoDL RTX 4090"
    assert updated["gpus"] == "1"
    assert updated["ssh_password_configured"] is True
    assert "never-return-this" not in repr(created)
    assert "never-return-this" not in repr(listed)
    assert "ssh_password" not in node_repository.nodes["autodl-gpu-01"]
    assert node_repository.secrets["autodl-gpu-01"]["ssh_password"] == (
        "never-return-this"
    )

    deleted = delete_remote_node_setting("autodl-gpu-01")

    assert deleted == {"id": "autodl-gpu-01", "deleted": True}
    assert list_remote_node_settings() == []


def test_remote_node_settings_persists_cpu_compute_type(
    node_repository,
) -> None:
    created = create_remote_node_setting(_node(
        id="autodl-cpu-01",
        name="AutoDL CPU",
        compute_type="cpu",
        gpus="0",
    ))

    assert created["compute_type"] == "cpu"
    assert created["gpus"] == "0"
    assert node_repository.nodes["autodl-cpu-01"]["compute_type"] == "cpu"


def test_remote_node_settings_rejects_invalid_duplicate_or_missing_secret() -> None:
    create_remote_node_setting(_node())

    with pytest.raises(ValueError, match="已存在"):
        create_remote_node_setting(_node())
    with pytest.raises(ValueError, match="host无效"):
        update_remote_node_setting(
            "autodl-gpu-01", _node(host="gpu;shutdown", ssh_password=""),
        )
    with pytest.raises(ValueError, match="SSH私钥"):
        create_remote_node_setting(_node(
            id="autodl-gpu-02",
            authentication_type="ssh_private_key",
            ssh_password="",
            ssh_private_key="",
        ))


def test_remote_node_settings_persists_autodl_token_and_preserves_blank_update(
    node_repository,
) -> None:
    created = create_remote_node_setting(_node(
        lifecycle_provider="autodl_pro",
        instance_uuid="pro-76576c61fdf1",
        api_token="saved-api-token",
        auto_start=True,
        auto_stop=True,
        boot_timeout_minutes=20,
    ))
    updated = update_remote_node_setting(
        "autodl-gpu-01",
        _node(
            lifecycle_provider="autodl_pro",
            instance_uuid="pro-76576c61fdf1",
            ssh_password="",
            api_token="",
            auto_start=True,
            auto_stop=True,
            boot_timeout_minutes=20,
        ),
    )

    assert created["api_token_configured"] is True
    assert created["api_token_source"] == "postgresql_encrypted"
    assert updated["api_token_configured"] is True
    assert "saved-api-token" not in repr(created)
    assert "saved-api-token" not in repr(updated)
    assert "api_token" not in node_repository.nodes["autodl-gpu-01"]
    assert node_repository.secrets["autodl-gpu-01"]["api_token"] == (
        "saved-api-token"
    )


def test_remote_node_settings_accepts_private_key_without_returning_it() -> None:
    private_key = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "dGVzdC1vbmx5\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )

    created = create_remote_node_setting(_node(
        authentication_type="ssh_private_key",
        ssh_password="",
        ssh_private_key=private_key,
    ))

    assert created["credential_type"] == "ssh_private_key"
    assert created["ssh_private_key_configured"] is True
    assert private_key not in repr(created)
