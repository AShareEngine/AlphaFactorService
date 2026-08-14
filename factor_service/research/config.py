from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from factor_service.runtime_config import (
    load_runtime_config,
    resolve_project_path,
    section,
)


@dataclass(frozen=True)
class Settings:
    clickhouse_host: str
    clickhouse_port: int
    clickhouse_user: str
    clickhouse_password: str
    factor_database: str
    model_database: str
    source_database: str
    work_root: Path
    model_artifacts_root: Path
    scheduler_enabled: bool = True
    scheduler_refresh_seconds: float = 60.0


def load_settings() -> Settings:
    runtime = load_runtime_config()
    storage = section(runtime, "research", "storage")
    scheduler = section(runtime, "research", "scheduler")
    clickhouse = section(runtime, "clickhouse")
    source = section(runtime, "sources", "research")
    clickhouse_host = str(clickhouse.get("host") or "127.0.0.1").strip()
    result = Settings(
        clickhouse_host=clickhouse_host,
        clickhouse_port=int(clickhouse.get("port") or 8123),
        clickhouse_user=str(clickhouse.get("username") or "default").strip(),
        clickhouse_password=str(clickhouse.get("password") or ""),
        factor_database=str(clickhouse.get("factor_database") or "ab_factor").strip(),
        model_database=str(clickhouse.get("model_database") or "ab_model").strip(),
        source_database=str(source.get("database") or "starlight").strip(),
        work_root=resolve_project_path(storage.get("work_root"), "data/research"),
        model_artifacts_root=resolve_project_path(
            storage.get("model_artifacts_root"), "data/model_artifacts"
        ),
        scheduler_enabled=bool(scheduler.get("enabled", True)),
        scheduler_refresh_seconds=max(
            10.0, float(scheduler.get("refresh_seconds") or 60.0),
        ),
    )
    return result
