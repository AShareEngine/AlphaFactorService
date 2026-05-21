from __future__ import annotations

from functools import lru_cache

import clickhouse_connect

from factor_service.config import Settings, load_settings


SCHEMA_STATEMENTS = [
    "CREATE DATABASE IF NOT EXISTS {database}",
    """
    CREATE TABLE IF NOT EXISTS {database}.factor_definitions
    (
        factor_id String,
        version UInt32,
        label String,
        description String,
        entity_type LowCardinality(String),
        category String,
        group_name String,
        output_type LowCardinality(String),
        frequency LowCardinality(String),
        required_fields Array(String),
        params_json String,
        expression String,
        enabled UInt8,
        created_at DateTime DEFAULT now(),
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (factor_id, version)
    """,
    """
    CREATE TABLE IF NOT EXISTS {database}.factor_compute_jobs
    (
        job_id String,
        factor_id String,
        factor_version UInt32,
        entity_type LowCardinality(String),
        mode LowCardinality(String),
        universe String,
        date_start Nullable(Date),
        date_end Nullable(Date),
        params_json String,
        status LowCardinality(String),
        error_message String,
        row_count Nullable(UInt64),
        created_at DateTime DEFAULT now(),
        started_at Nullable(DateTime),
        finished_at Nullable(DateTime),
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (job_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS {database}.factor_values_daily
    (
        trade_date Date,
        entity_type LowCardinality(String),
        entity_code String,
        factor_id LowCardinality(String),
        factor_version UInt32,
        params_hash String,
        raw_value Nullable(Float64),
        rank_value Nullable(UInt32),
        percentile Nullable(Float64),
        score Nullable(Float64),
        job_id String,
        updated_at DateTime DEFAULT now()
    )
    ENGINE = MergeTree
    PARTITION BY toYYYYMM(trade_date)
    ORDER BY (factor_id, trade_date, entity_code, params_hash)
    """,
]


@lru_cache(maxsize=1)
def settings() -> Settings:
    return load_settings()


@lru_cache(maxsize=1)
def client():
    config = settings()
    return clickhouse_connect.get_client(
        host=config.clickhouse_host,
        port=config.clickhouse_port,
        username=config.clickhouse_user,
        password=config.clickhouse_password,
    )


def init_schema() -> None:
    config = settings()
    db_client = client()
    for statement in SCHEMA_STATEMENTS:
        db_client.command(statement.format(database=config.clickhouse_database))
