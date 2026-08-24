from __future__ import annotations

import os
import stat
import threading
from typing import Any

import yaml

from factor_service.research.autodl import api_token_status, save_api_token
from factor_service.research.remote import RemoteNode, load_remote_nodes
from factor_service.runtime_config import load_runtime_config, runtime_config_path


_CONFIG_LOCK = threading.RLock()


def list_remote_node_settings() -> list[dict[str, Any]]:
    runtime = load_runtime_config()
    raw_nodes = _raw_remote_nodes(runtime)
    normalized = {node.node_id: node for node in load_remote_nodes(runtime)}
    return [
        _settings_view(normalized[str(raw.get("id") or "").strip()])
        for raw in raw_nodes
    ]


def create_remote_node_setting(payload: dict[str, Any]) -> dict[str, Any]:
    node_id = str(payload.get("id") or "").strip()
    if not node_id:
        raise ValueError("远程训练节点ID不能为空")
    with _CONFIG_LOCK:
        runtime = load_runtime_config()
        nodes = _raw_remote_nodes(runtime)
        if any(str(item.get("id") or "").strip() == node_id for item in nodes):
            raise ValueError(f"远程训练节点已存在: {node_id}")
        nodes.append(_submitted_node(node_id, payload))
        saved = _validate_and_write(runtime, nodes)
        _save_submitted_api_token(payload)
    return _settings_view(saved[node_id])


def update_remote_node_setting(
    node_id: str, payload: dict[str, Any],
) -> dict[str, Any]:
    clean_id = str(node_id or "").strip()
    with _CONFIG_LOCK:
        runtime = load_runtime_config()
        nodes = _raw_remote_nodes(runtime)
        index = next((
            index for index, item in enumerate(nodes)
            if str(item.get("id") or "").strip() == clean_id
        ), None)
        if index is None:
            raise ValueError(f"远程训练节点未配置: {clean_id}")
        submitted_id = str(payload.get("id") or clean_id).strip()
        if submitted_id != clean_id:
            raise ValueError("远程训练节点ID创建后不可修改")
        nodes[index] = _submitted_node(clean_id, payload)
        saved = _validate_and_write(runtime, nodes)
        _save_submitted_api_token(payload)
    return _settings_view(saved[clean_id])


def _save_submitted_api_token(payload: dict[str, Any]) -> None:
    if str(payload.get("lifecycle_provider") or "").strip() != "autodl_pro":
        return
    token = str(payload.get("api_token") or "").strip()
    if token:
        save_api_token(token)


def delete_remote_node_setting(node_id: str) -> dict[str, Any]:
    clean_id = str(node_id or "").strip()
    with _CONFIG_LOCK:
        runtime = load_runtime_config()
        nodes = _raw_remote_nodes(runtime)
        remaining = [
            item for item in nodes
            if str(item.get("id") or "").strip() != clean_id
        ]
        if len(remaining) == len(nodes):
            raise ValueError(f"远程训练节点未配置: {clean_id}")
        _validate_and_write(runtime, remaining)
    return {"id": clean_id, "deleted": True}


def _raw_remote_nodes(runtime: dict[str, Any]) -> list[dict[str, Any]]:
    research = runtime.get("research") or {}
    if not isinstance(research, dict):
        raise ValueError("research配置必须是对象")
    execution = research.get("execution") or {}
    if not isinstance(execution, dict):
        raise ValueError("research.execution配置必须是对象")
    nodes = execution.get("remote_nodes") or []
    if not isinstance(nodes, list):
        raise ValueError("research.execution.remote_nodes必须是数组")
    if any(not isinstance(item, dict) for item in nodes):
        raise ValueError("远程训练节点配置必须是对象")
    return [dict(item) for item in nodes]


def _submitted_node(node_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node_id,
        "name": str(payload.get("name") or node_id).strip(),
        "enabled": _boolean(payload.get("enabled"), True),
        "host": str(payload.get("host") or "").strip(),
        "port": int(payload.get("port") or 22),
        "user": str(payload.get("user") or "root").strip(),
        "ssh_key": str(payload.get("ssh_key") or "").strip(),
        "password_env": str(payload.get("password_env") or "").strip(),
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
        "gpus": str(payload.get("gpus") or "all").strip(),
        "max_runtime_minutes": int(payload.get("max_runtime_minutes") or 240),
        "cleanup_success": _boolean(payload.get("cleanup_success"), True),
        "lifecycle_provider": str(
            payload.get("lifecycle_provider") or ""
        ).strip().lower(),
        "instance_uuid": str(payload.get("instance_uuid") or "").strip(),
        "api_token_env": str(payload.get("api_token_env") or "").strip(),
        "auto_start": _boolean(payload.get("auto_start"), False),
        "auto_stop": _boolean(payload.get("auto_stop"), False),
        "boot_timeout_minutes": int(
            payload.get("boot_timeout_minutes") or 15
        ),
    }


def _validate_and_write(
    runtime: dict[str, Any], nodes: list[dict[str, Any]],
) -> dict[str, RemoteNode]:
    updated = dict(runtime)
    research = dict(updated.get("research") or {})
    execution = dict(research.get("execution") or {})
    execution["remote_nodes"] = nodes
    research["execution"] = execution
    updated["research"] = research
    normalized = load_remote_nodes(updated)
    _atomic_write_runtime(updated)
    return {node.node_id: node for node in normalized}


def _atomic_write_runtime(runtime: dict[str, Any]) -> None:
    target = runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    original_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        serialized = yaml.safe_dump(
            runtime, allow_unicode=True, sort_keys=False, default_flow_style=False,
        )
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(original_mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _settings_view(node: RemoteNode) -> dict[str, Any]:
    token_status = (
        api_token_status(node.api_token_env)
        if node.api_token_env else {"configured": False, "source": "none"}
    )
    return {
        **node.public(),
        "ssh_key": str(node.ssh_key or ""),
        "password_env": node.password_env,
        "password_environment_configured": bool(
            node.password_env and os.environ.get(node.password_env)
        ),
        "known_hosts": str(node.known_hosts or ""),
        "cleanup_success": node.cleanup_success,
        "max_runtime_minutes": node.max_runtime_minutes,
        "api_token_env": node.api_token_env,
        "api_token_configured": bool(token_status["configured"]),
        "api_token_environment_configured": bool(token_status["configured"]),
        "api_token_source": token_status["source"],
        "lifecycle_provider": node.lifecycle_provider or "manual",
        "instance_uuid": node.instance_uuid,
        "auto_start": node.auto_start,
        "auto_stop": node.auto_stop,
        "boot_timeout_minutes": node.boot_timeout_minutes,
        "configuration_valid": True,
        "authentication_hint": (
            "SSH私钥" if node.ssh_key is not None
            else f"环境变量 {node.password_env}"
        ),
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
