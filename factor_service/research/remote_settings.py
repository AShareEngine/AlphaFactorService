from __future__ import annotations

import threading
from typing import Any

from factor_service.research.remote import (
    RemoteNode,
    load_remote_nodes,
    remote_node_storage_payload,
)
from factor_service.research.remote_node_repository import (
    get_remote_node_repository,
)


_CONFIG_LOCK = threading.RLock()


def list_remote_node_settings() -> list[dict[str, Any]]:
    return [_settings_view(node) for node in load_remote_nodes()]


def create_remote_node_setting(payload: dict[str, Any]) -> dict[str, Any]:
    node_id = str(payload.get("id") or "").strip()
    if not node_id:
        raise ValueError("远程训练节点ID不能为空")
    with _CONFIG_LOCK:
        load_remote_nodes()
        node = _validated_submitted_node(node_id, payload)
        _require_secrets(node)
        stored = get_remote_node_repository().create_node(
            remote_node_storage_payload(node),
            _node_secrets(node),
        )
    return _settings_view(_validated_stored_node(stored))


def update_remote_node_setting(
    node_id: str, payload: dict[str, Any],
) -> dict[str, Any]:
    clean_id = str(node_id or "").strip()
    with _CONFIG_LOCK:
        submitted_id = str(payload.get("id") or clean_id).strip()
        if submitted_id != clean_id:
            raise ValueError("远程训练节点ID创建后不可修改")
        existing = next(
            (node for node in load_remote_nodes() if node.node_id == clean_id),
            None,
        )
        if existing is None:
            raise ValueError(f"远程训练节点未配置: {clean_id}")
        node = _validated_submitted_node(clean_id, payload, existing=existing)
        _require_secrets(node)
        stored = get_remote_node_repository().update_node(
            clean_id, remote_node_storage_payload(node),
            _node_secrets(node),
        )
    return _settings_view(_validated_stored_node(stored))


def delete_remote_node_setting(node_id: str) -> dict[str, Any]:
    clean_id = str(node_id or "").strip()
    with _CONFIG_LOCK:
        load_remote_nodes()
        if not get_remote_node_repository().delete_node(clean_id):
            raise ValueError(f"远程训练节点未配置: {clean_id}")
    return {"id": clean_id, "deleted": True}


def _validated_submitted_node(
    node_id: str,
    payload: dict[str, Any],
    *,
    existing: RemoteNode | None = None,
) -> RemoteNode:
    return _validated_stored_node(_submitted_node(node_id, payload, existing))


def _validated_stored_node(payload: dict[str, Any]) -> RemoteNode:
    runtime = {"research": {"execution": {"remote_nodes": [payload]}}}
    return load_remote_nodes(runtime)[0]


def _submitted_node(
    node_id: str,
    payload: dict[str, Any],
    existing: RemoteNode | None,
) -> dict[str, Any]:
    authentication_type = str(
        payload.get("authentication_type")
        or (existing.authentication_type if existing else "ssh_private_key")
    ).strip().lower()
    ssh_private_key = str(payload.get("ssh_private_key") or "")
    ssh_password = str(payload.get("ssh_password") or "")
    if existing and authentication_type == existing.authentication_type:
        if authentication_type == "ssh_private_key" and not ssh_private_key:
            ssh_private_key = existing.ssh_private_key
        if authentication_type == "password" and not ssh_password:
            ssh_password = existing.ssh_password
    lifecycle_provider = str(
        payload.get("lifecycle_provider") or ""
    ).strip().lower()
    api_token = str(payload.get("api_token") or "").strip()
    if (
        existing
        and lifecycle_provider == "autodl_pro"
        and existing.lifecycle_provider == "autodl_pro"
        and not api_token
    ):
        api_token = existing.api_token
    compute_type = str(
        payload.get("compute_type")
        or (existing.compute_type if existing else "")
    ).strip().lower()
    if payload.get("gpus") is not None and payload.get("gpus") != "":
        gpus = str(payload["gpus"]).strip()
    elif existing and compute_type == existing.compute_type:
        gpus = existing.gpus
    else:
        gpus = "0" if compute_type == "cpu" else "all"
    return {
        "id": node_id,
        "name": str(payload.get("name") or node_id).strip(),
        "enabled": _boolean(payload.get("enabled"), True),
        "host": str(payload.get("host") or "").strip(),
        "port": int(payload.get("port") or 22),
        "user": str(payload.get("user") or "root").strip(),
        "authentication_type": authentication_type,
        "ssh_private_key": ssh_private_key,
        "ssh_password": ssh_password,
        "known_hosts": str(payload.get("known_hosts") or "").strip(),
        "work_dir": str(
            payload.get("work_dir") or "/root/alphablocks-research"
        ).strip(),
        "runner": str(payload.get("runner") or "docker").strip(),
        "python_executable": str(
            payload.get("python_executable") or "python"
        ).strip(),
        "docker_image": str(
            payload.get("docker_image") or "alphafactor-research:latest"
        ).strip(),
        "compute_type": compute_type,
        "gpus": gpus,
        "max_runtime_minutes": int(payload.get("max_runtime_minutes") or 240),
        "cleanup_success": _boolean(payload.get("cleanup_success"), True),
        "lifecycle_provider": lifecycle_provider,
        "instance_uuid": str(payload.get("instance_uuid") or "").strip(),
        "api_token": api_token,
        "auto_start": _boolean(payload.get("auto_start"), False),
        "auto_stop": _boolean(payload.get("auto_stop"), False),
        "boot_timeout_minutes": int(
            payload.get("boot_timeout_minutes") or 15
        ),
    }


def _settings_view(node: RemoteNode) -> dict[str, Any]:
    return {
        **node.public(),
        "ssh_private_key_configured": bool(node.ssh_private_key),
        "ssh_password_configured": bool(node.ssh_password),
        "known_hosts": node.known_hosts_config or str(node.known_hosts or ""),
        "cleanup_success": node.cleanup_success,
        "max_runtime_minutes": node.max_runtime_minutes,
        "api_token_configured": bool(node.api_token),
        "api_token_source": "postgresql_encrypted" if node.api_token else "none",
        "lifecycle_provider": node.lifecycle_provider or "manual",
        "instance_uuid": node.instance_uuid,
        "auto_start": node.auto_start,
        "auto_stop": node.auto_stop,
        "boot_timeout_minutes": node.boot_timeout_minutes,
        "configuration_valid": True,
        "authentication_hint": (
            "PostgreSQL加密SSH私钥"
            if node.authentication_type == "ssh_private_key"
            else "PostgreSQL加密SSH密码"
        ),
    }


def _require_secrets(node: RemoteNode) -> None:
    if not node.credentials_available():
        label = "SSH私钥" if node.authentication_type == "ssh_private_key" else "SSH密码"
        raise ValueError(f"远程训练节点{node.node_id}必须配置{label}")
    if node.lifecycle_provider == "autodl_pro" and not node.api_token:
        raise ValueError("AutoDL Pro节点必须配置开发者Token")


def _node_secrets(node: RemoteNode) -> dict[str, str]:
    return {
        "ssh_private_key": node.ssh_private_key,
        "ssh_password": node.ssh_password,
        "api_token": node.api_token,
    }


def _boolean(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"无法解析布尔值: {value!r}")


__all__ = [
    "create_remote_node_setting",
    "delete_remote_node_setting",
    "list_remote_node_settings",
    "update_remote_node_setting",
]
