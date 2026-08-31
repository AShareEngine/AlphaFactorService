from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from factor_service.model_object_store import ModelObjectStoreConfig, load_secret_env
from factor_service.runtime_config import (
    as_bool,
    load_runtime_config,
    resolve_project_path,
    section,
    string_list,
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
    model_object_store: ModelObjectStoreConfig = ModelObjectStoreConfig()
    dataset_cache_retention_hours: float = 24.0
    dataset_cache_cleanup_interval_seconds: float = 3600.0
    scheduler_enabled: bool = True
    scheduler_refresh_seconds: float = 60.0
    experiment_worker_enabled: bool = True
    factor_query_chunk_days: int = 90
    stock_daily_table: str = "stock_daily_factor_source"
    data_sdk_api_base_url: str = ""
    data_sdk_query_timeout_seconds: float = 120.0
    data_sdk_query_concurrency: int = 4


def load_settings() -> Settings:
    runtime = load_runtime_config()
    storage = section(runtime, "research", "storage")
    scheduler = section(runtime, "research", "scheduler")
    worker = section(runtime, "research", "worker")
    dataset = section(runtime, "research", "dataset")
    clickhouse = section(runtime, "clickhouse")
    source = section(runtime, "sources", "research")
    factor_source = section(runtime, "sources", "factor")
    object_store = section(runtime, "research", "storage", "object_store")
    object_store_enabled = as_bool(object_store.get("enabled"), False)
    object_store_secrets: dict[str, str] = {}
    if object_store_enabled:
        credentials_file = resolve_project_path(
            object_store.get("credentials_file"), ".secrets/alphablocks-s3.env",
        )
        object_store_secrets = load_secret_env(credentials_file)
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
        dataset_cache_retention_hours=max(
            1.0, float(storage.get("dataset_cache_retention_hours") or 24.0),
        ),
        dataset_cache_cleanup_interval_seconds=max(
            60.0,
            float(storage.get("dataset_cache_cleanup_interval_seconds") or 3600.0),
        ),
        model_object_store=ModelObjectStoreConfig(
            enabled=object_store_enabled,
            endpoint_url=str(
                object_store.get("endpoint_url")
                or object_store_secrets.get("S3_ENDPOINT")
                or ""
            ).strip().rstrip("/"),
            bucket=str(
                object_store.get("bucket")
                or object_store_secrets.get("S3_BUCKET")
                or ""
            ).strip(),
            region=str(
                object_store.get("region")
                or object_store_secrets.get("S3_REGION")
                or "us-east-1"
            ).strip(),
            access_key=str(
                object_store_secrets.get("MINIO_APP_ACCESS_KEY")
                or object_store_secrets.get("AWS_ACCESS_KEY_ID")
                or ""
            ).strip(),
            secret_key=str(
                object_store_secrets.get("MINIO_APP_SECRET_KEY")
                or object_store_secrets.get("AWS_SECRET_ACCESS_KEY")
                or ""
            ).strip(),
            prefix=str(object_store.get("prefix") or "models").strip(),
            artifact_kinds=string_list(
                object_store.get("artifact_kinds"),
                ("bundle", "walk_forward_series"),
            ),
        ),
        scheduler_enabled=bool(scheduler.get("enabled", True)),
        scheduler_refresh_seconds=max(
            10.0, float(scheduler.get("refresh_seconds") or 60.0),
        ),
        experiment_worker_enabled=bool(worker.get("enabled", True)),
        factor_query_chunk_days=max(
            30, min(int(dataset.get("factor_query_chunk_days") or 90), 366),
        ),
        stock_daily_table=str(
            factor_source.get("stock_daily_table")
            or "stock_daily_factor_source"
        ).strip(),
        data_sdk_api_base_url=str(
            factor_source.get("entity_asset_api_base_url") or ""
        ).strip().rstrip("/"),
        data_sdk_query_timeout_seconds=max(
            1.0,
            float(
                factor_source.get("entity_asset_query_timeout_seconds") or 120
            ),
        ),
        data_sdk_query_concurrency=max(
            1,
            int(factor_source.get("entity_asset_query_concurrency") or 4),
        ),
    )
    return result
