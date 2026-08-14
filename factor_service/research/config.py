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
    api_url: str
    worker_token: str
    clickhouse_host: str
    clickhouse_port: int
    clickhouse_user: str
    clickhouse_password: str
    factor_database: str
    model_database: str
    source_database: str
    work_root: Path
    model_artifacts_root: Path
    service_host: str
    service_port: int


def load_settings() -> Settings:
    runtime = load_runtime_config()
    research = section(runtime, "research")
    storage = section(runtime, "research", "storage")
    clickhouse = section(runtime, "clickhouse")
    source = section(runtime, "sources", "research")
    api_url = str(
        research.get("api_url") or "http://127.0.0.1:8001/api/model-research"
    ).strip().rstrip("/")
    clickhouse_host = str(clickhouse.get("host") or "127.0.0.1").strip()
    service_host = str(research.get("listen_host") or "127.0.0.1").strip()
    service_port = int(research.get("listen_port") or 8787)
    result = Settings(
        api_url=api_url,
        worker_token=str(research.get("token") or ""),
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
        service_host=service_host,
        service_port=service_port,
    )
    if not 1 <= result.service_port <= 65535:
        raise ValueError("research.listen_port必须在1到65535之间")
    return result
