from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml


DEFAULT_FACTOR_DATABASE = "ab_factor"
DEFAULT_SYNC_RUNTIME_CONFIG = (
    Path(__file__).resolve().parents[2] / "AlphaBlocksSyncData" / "config" / "runtime.local.yaml"
)


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    cors_origins: tuple[str, ...]
    clickhouse_host: str
    clickhouse_port: int
    clickhouse_user: str
    clickhouse_password: str
    clickhouse_database: str
    clickhouse_source_database: str
    clickhouse_secure: bool


def load_settings() -> Settings:
    _load_dotenv()
    runtime = _load_runtime_payload()
    datasource = _mapping(runtime.get("datasource"))
    return Settings(
        host=_env("AB_FACTOR_HOST", "127.0.0.1"),
        port=int(_env("AB_FACTOR_PORT", "8100")),
        cors_origins=tuple(
            item.strip()
            for item in _env("AB_FACTOR_CORS_ORIGINS", "*").split(",")
            if item.strip()
        ),
        clickhouse_host=_env("AB_FACTOR_CLICKHOUSE_HOST", str(datasource.get("host") or "127.0.0.1")),
        clickhouse_port=int(_env("AB_FACTOR_CLICKHOUSE_PORT", str(datasource.get("port") or "8123"))),
        clickhouse_user=_env("AB_FACTOR_CLICKHOUSE_USER", str(datasource.get("username") or "default")),
        clickhouse_password=_env("AB_FACTOR_CLICKHOUSE_PASSWORD", str(datasource.get("password") or "")),
        clickhouse_database=_env("AB_FACTOR_CLICKHOUSE_DATABASE", DEFAULT_FACTOR_DATABASE),
        clickhouse_source_database=str(datasource.get("database") or ""),
        clickhouse_secure=_env_bool("AB_FACTOR_CLICKHOUSE_SECURE", _as_bool(datasource.get("secure"), False)),
    )


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return _as_bool(value, default)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _load_runtime_payload() -> Mapping[str, Any]:
    path = _runtime_config_path()
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    return payload if isinstance(payload, Mapping) else {}


def _runtime_config_path() -> Optional[Path]:
    explicit_path = (
        os.environ.get("AB_FACTOR_RUNTIME_CONFIG")
        or os.environ.get("SYNC_DATA_RUNTIME_CONFIG")
        or os.environ.get("ALPHABLOCKS_SYNC_DATA_RUNTIME_CONFIG")
        or os.environ.get("ALPHABLOCKS_RUNTIME_CONFIG")
        or os.environ.get("RUNTIME_CONFIG_PATH")
    )
    if explicit_path:
        return Path(explicit_path).expanduser().resolve()
    if DEFAULT_SYNC_RUNTIME_CONFIG.exists():
        return DEFAULT_SYNC_RUNTIME_CONFIG
    return None


def _load_dotenv() -> None:
    path = Path.cwd() / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
