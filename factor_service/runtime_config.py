from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_CONFIG_PATH = PROJECT_ROOT / "config" / "runtime.local.yaml"


def runtime_config_path() -> Path:
    configured = str(os.environ.get("ALPHA_FACTOR_RUNTIME_CONFIG") or "").strip()
    path = Path(configured) if configured else DEFAULT_RUNTIME_CONFIG_PATH
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def load_runtime_config(path: str | Path | None = None) -> dict[str, Any]:
    resolved = _resolve_config_path(path)
    if not resolved.is_file():
        raise FileNotFoundError(
            f"运行配置不存在: {resolved}；请从config/runtime.example.yaml复制创建"
            "config/runtime.local.yaml"
        )
    payload = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"运行配置必须是YAML对象: {resolved}")
    return payload


def section(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    if current is None:
        return {}
    if not isinstance(current, dict):
        raise ValueError(f"配置项{'.'.join(keys)}必须是对象")
    return current


def resolve_project_path(value: Any, default: str) -> Path:
    text = str(value or default).strip()
    if not text:
        raise ValueError("存储路径不能为空")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"无法解析布尔配置值: {value!r}")


def string_list(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    items = value if isinstance(value, list) else str(value).split(",")
    result = tuple(str(item).strip() for item in items if str(item).strip())
    return result or default


def _resolve_config_path(path: str | Path | None) -> Path:
    if path is None:
        resolved = runtime_config_path()
    else:
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = PROJECT_ROOT / resolved
        resolved = resolved.resolve()
    return resolved


__all__ = [
    "DEFAULT_RUNTIME_CONFIG_PATH",
    "PROJECT_ROOT",
    "as_bool",
    "load_runtime_config",
    "resolve_project_path",
    "runtime_config_path",
    "section",
    "string_list",
]
