from __future__ import annotations

from functools import lru_cache
from threading import local

import clickhouse_connect

from factor_service.config import Settings, load_settings


_client_state = local()


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
        asset_id String DEFAULT entity_type,
        source_node_id String DEFAULT if(entity_type = 'stock' AND frequency = 'daily', 'stock_daily_real', ''),
        required_fields Array(String),
        params_json String,
        param_schema_json String DEFAULT '{{}}',
        availability_policy_json String DEFAULT '{{"field":"available_at","policy":"persisted_timestamp"}}',
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
        event_available_at DateTime('Asia/Shanghai')
            DEFAULT toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR,
        updated_at DateTime DEFAULT now(),
        available_at DateTime('Asia/Shanghai')
            DEFAULT toTimeZone(updated_at, 'Asia/Shanghai'),
        computed_at DateTime('Asia/Shanghai')
            DEFAULT toTimeZone(updated_at, 'Asia/Shanghai'),
        source_vintage String DEFAULT concat(
            'legacy#',
            job_id,
            '@',
            formatDateTime(
                toTimeZone(updated_at, 'Asia/Shanghai'),
                '%Y-%m-%dT%H:%i:%S'
            )
        )
    )
    ENGINE = MergeTree
    PARTITION BY toYYYYMM(trade_date)
    ORDER BY (factor_id, trade_date, entity_code, params_hash)
    """,
    """
    CREATE TABLE IF NOT EXISTS {database}.factor_analysis_jobs
    (
        analysis_job_id String,
        factor_id String,
        factor_version UInt32,
        entity_type LowCardinality(String),
        params_hash String,
        date_start Nullable(Date),
        date_end Nullable(Date),
        periods Array(UInt32),
        quantiles UInt8,
        price_field String,
        cumulative_returns UInt8,
        max_loss Float64,
        status LowCardinality(String),
        error_message String,
        row_count Nullable(UInt64),
        created_at DateTime DEFAULT now(),
        started_at Nullable(DateTime),
        finished_at Nullable(DateTime),
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (analysis_job_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS {database}.factor_analysis_summary
    (
        analysis_job_id String,
        metric String,
        period String,
        value Nullable(Float64),
        payload_json String,
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (analysis_job_id, metric, period)
    """,
    """
    CREATE TABLE IF NOT EXISTS {database}.factor_analysis_ic_daily
    (
        analysis_job_id String,
        trade_date Date,
        period String,
        ic Nullable(Float64),
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (analysis_job_id, period, trade_date)
    """,
    """
    CREATE TABLE IF NOT EXISTS {database}.factor_analysis_quantile_returns
    (
        analysis_job_id String,
        trade_date Date,
        period String,
        quantile UInt8,
        mean_return Nullable(Float64),
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (analysis_job_id, period, quantile, trade_date)
    """,
    """
    CREATE TABLE IF NOT EXISTS {database}.factor_backtest_jobs
    (
        backtest_job_id String,
        factor_ids Array(String),
        universe_id LowCardinality(String),
        benchmark_code String,
        date_preset LowCardinality(String),
        requested_date_start Nullable(Date),
        requested_date_end Nullable(Date),
        date_start Nullable(Date),
        date_end Nullable(Date),
        quantiles UInt8 DEFAULT 5,
        signal_field LowCardinality(String) DEFAULT 'score',
        rebalance_frequency LowCardinality(String) DEFAULT 'daily',
        execution_price LowCardinality(String) DEFAULT 'next_open_backward_adjusted',
        buy_cost_rate Float64 DEFAULT 0.0003,
        sell_cost_rate Float64 DEFAULT 0.0013,
        configuration_json String DEFAULT '{{}}',
        status LowCardinality(String),
        error_message String,
        completed_factors UInt32 DEFAULT 0,
        total_factors UInt32 DEFAULT 0,
        created_at DateTime DEFAULT now(),
        started_at Nullable(DateTime),
        finished_at Nullable(DateTime),
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY backtest_job_id
    """,
    """
    CREATE TABLE IF NOT EXISTS {database}.factor_backtest_summary
    (
        backtest_job_id String,
        factor_id String,
        factor_version UInt32,
        params_hash String,
        status LowCardinality(String),
        error_message String,
        annual_return Nullable(Float64),
        excess_annual_return Nullable(Float64),
        long_short_annual_return Nullable(Float64),
        turnover_rate Nullable(Float64),
        ic_mean Nullable(Float64),
        ic_ir Nullable(Float64),
        max_drawdown Nullable(Float64),
        trading_days UInt32 DEFAULT 0,
        sample_days UInt32 DEFAULT 0,
        payload_json String DEFAULT '{{}}',
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (backtest_job_id, factor_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS {database}.factor_backtest_daily
    (
        backtest_job_id String,
        factor_id String,
        trade_date Date,
        q1_return Nullable(Float64),
        q5_return Nullable(Float64),
        long_short_return Nullable(Float64),
        benchmark_return Nullable(Float64),
        excess_return Nullable(Float64),
        q1_nav Nullable(Float64),
        q5_nav Nullable(Float64),
        long_short_nav Nullable(Float64),
        benchmark_nav Nullable(Float64),
        turnover Nullable(Float64),
        transaction_cost Nullable(Float64),
        ic Nullable(Float64),
        sample_count UInt32 DEFAULT 0,
        blocked_buy_count UInt32 DEFAULT 0,
        blocked_sell_count UInt32 DEFAULT 0,
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    PARTITION BY toYYYYMM(trade_date)
    ORDER BY (backtest_job_id, factor_id, trade_date)
    """,
    """
    CREATE TABLE IF NOT EXISTS {database}.factor_analysis_turnover_daily
    (
        analysis_job_id String,
        trade_date Date,
        period String,
        quantile UInt8,
        turnover Nullable(Float64),
        rank_autocorrelation Nullable(Float64),
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (analysis_job_id, period, quantile, trade_date)
    """,
    """
    ALTER TABLE {database}.factor_definitions
    ADD COLUMN IF NOT EXISTS asset_id String DEFAULT entity_type
    AFTER frequency
    """,
    """
    ALTER TABLE {database}.factor_definitions
    ADD COLUMN IF NOT EXISTS source_node_id String
    DEFAULT if(entity_type = 'stock' AND frequency = 'daily', 'stock_daily_real', '')
    AFTER asset_id
    """,
    """
    ALTER TABLE {database}.factor_definitions
    ADD COLUMN IF NOT EXISTS param_schema_json String DEFAULT '{{}}'
    AFTER params_json
    """,
    """
    ALTER TABLE {database}.factor_definitions
    ADD COLUMN IF NOT EXISTS availability_policy_json String
    DEFAULT '{{"field":"available_at","policy":"persisted_timestamp"}}'
    AFTER param_schema_json
    """,
    """
    ALTER TABLE {database}.factor_values_daily
    ADD COLUMN IF NOT EXISTS available_at DateTime('Asia/Shanghai')
    DEFAULT toTimeZone(updated_at, 'Asia/Shanghai')
    AFTER job_id
    """,
    """
    ALTER TABLE {database}.factor_values_daily
    MODIFY COLUMN available_at DateTime('Asia/Shanghai')
    DEFAULT toTimeZone(updated_at, 'Asia/Shanghai')
    """,
    """
    ALTER TABLE {database}.factor_values_daily
    ADD COLUMN IF NOT EXISTS event_available_at DateTime('Asia/Shanghai')
    DEFAULT toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR
    AFTER job_id
    """,
    """
    ALTER TABLE {database}.factor_values_daily
    ADD COLUMN IF NOT EXISTS computed_at DateTime('Asia/Shanghai')
    DEFAULT toTimeZone(updated_at, 'Asia/Shanghai')
    AFTER available_at
    """,
    """
    ALTER TABLE {database}.factor_values_daily
    ADD COLUMN IF NOT EXISTS source_vintage String DEFAULT concat(
        'legacy#',
        job_id,
        '@',
        formatDateTime(
            toTimeZone(updated_at, 'Asia/Shanghai'),
            '%Y-%m-%dT%H:%i:%S'
        )
    )
    AFTER computed_at
    """,
    """
    ALTER TABLE {database}.factor_values_daily
    MODIFY COLUMN source_vintage String DEFAULT concat(
        'legacy#',
        job_id,
        '@',
        formatDateTime(
            toTimeZone(updated_at, 'Asia/Shanghai'),
            '%Y-%m-%dT%H:%i:%S'
        )
    )
    """,
]


MODEL_SCHEMA_STATEMENTS = [
    "CREATE DATABASE IF NOT EXISTS {database}",
    """
    CREATE TABLE IF NOT EXISTS {database}.model_predictions_daily
    (
        trade_date Date,
        entity_type LowCardinality(String) DEFAULT 'stock',
        entity_code String,
        model_id String,
        model_version UInt32,
        raw_prediction Float64,
        rank_value UInt32,
        percentile Float64,
        score Float64,
        feature_cutoff_at DateTime('Asia/Shanghai'),
        computed_at DateTime('Asia/Shanghai'),
        source_vintage String,
        dataset_hash String,
        inference_run_id String,
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    PARTITION BY toYYYYMM(trade_date)
    ORDER BY (model_id, model_version, trade_date, entity_code, inference_run_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS {database}.model_backtest_jobs
    (
        backtest_job_id String,
        model_id String,
        model_version UInt32,
        universe_id LowCardinality(String),
        benchmark_code String,
        date_preset LowCardinality(String),
        requested_date_start Nullable(Date),
        requested_date_end Nullable(Date),
        date_start Nullable(Date),
        date_end Nullable(Date),
        top_n UInt32 DEFAULT 20,
        rebalance_every UInt32 DEFAULT 5,
        buy_cost_rate Float64 DEFAULT 0.0003,
        sell_cost_rate Float64 DEFAULT 0.0013,
        configuration_json String DEFAULT '{{}}',
        status LowCardinality(String),
        error_message String,
        annual_return Nullable(Float64),
        excess_annual_return Nullable(Float64),
        sharpe_ratio Nullable(Float64),
        turnover_rate Nullable(Float64),
        max_drawdown Nullable(Float64),
        trading_days UInt32 DEFAULT 0,
        payload_json String DEFAULT '{{}}',
        created_at DateTime DEFAULT now(),
        started_at Nullable(DateTime),
        finished_at Nullable(DateTime),
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY backtest_job_id
    """,
    """
    CREATE TABLE IF NOT EXISTS {database}.model_backtest_daily
    (
        backtest_job_id String,
        trade_date Date,
        portfolio_return Nullable(Float64),
        benchmark_return Nullable(Float64),
        excess_return Nullable(Float64),
        portfolio_nav Nullable(Float64),
        benchmark_nav Nullable(Float64),
        turnover Nullable(Float64),
        transaction_cost Nullable(Float64),
        sample_count UInt32 DEFAULT 0,
        holding_count UInt32 DEFAULT 0,
        blocked_buy_count UInt32 DEFAULT 0,
        blocked_sell_count UInt32 DEFAULT 0,
        holdings_json String DEFAULT '[]',
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    PARTITION BY toYYYYMM(trade_date)
    ORDER BY (backtest_job_id, trade_date)
    """,
]


@lru_cache(maxsize=1)
def settings() -> Settings:
    return load_settings()


def client():
    """Return one ClickHouse client per worker thread.

    FastAPI executes synchronous endpoints in a thread pool.  A
    clickhouse-connect client owns a session and rejects concurrent queries in
    that session, so sharing one globally makes simultaneous page requests
    fail intermittently.
    """
    cached = getattr(_client_state, "client", None)
    if cached is not None:
        return cached

    config = settings()
    cached = clickhouse_connect.get_client(
        host=config.clickhouse_host,
        port=config.clickhouse_port,
        username=config.clickhouse_user,
        password=config.clickhouse_password,
        secure=config.clickhouse_secure,
        autogenerate_session_id=False,
    )
    _client_state.client = cached
    return cached


def init_schema() -> None:
    config = settings()
    db_client = client()
    for statement in SCHEMA_STATEMENTS:
        db_client.command(statement.format(database=config.clickhouse_database))
    for statement in MODEL_SCHEMA_STATEMENTS:
        db_client.command(statement.format(database=config.model_database))
    _migrate_legacy_available_at(db_client, config.clickhouse_database)


def _migrate_legacy_available_at(db_client, database: str) -> None:
    """Keep old event timestamps while making available_at truly persisted-time.

    Renaming a ClickHouse column is metadata-only. The replacement column uses
    updated_at as its default, so old rows do not need a 70M-row mutation.
    """
    table = f"{database}.factor_values_daily"
    columns = {
        str(row[0])
        for row in db_client.query(f"DESCRIBE TABLE {table}").result_rows
    }
    if "legacy_available_at" in columns or "available_at" not in columns:
        return
    mismatched = int(
        db_client.query(
            f"SELECT countIf(available_at != computed_at) FROM {table}"
        ).result_rows[0][0]
        or 0
    )
    if mismatched < 1:
        return
    db_client.command(
        f"ALTER TABLE {table} "
        "RENAME COLUMN available_at TO legacy_available_at"
    )
    db_client.command(
        f"ALTER TABLE {table} "
        "ADD COLUMN available_at DateTime('Asia/Shanghai') "
        "DEFAULT toTimeZone(updated_at, 'Asia/Shanghai') "
        "AFTER event_available_at"
    )
