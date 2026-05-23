CREATE DATABASE IF NOT EXISTS ab_factor;

CREATE TABLE IF NOT EXISTS ab_factor.factor_definitions
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
ORDER BY (factor_id, version);

CREATE TABLE IF NOT EXISTS ab_factor.factor_compute_jobs
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
ORDER BY (job_id);

CREATE TABLE IF NOT EXISTS ab_factor.factor_values_daily
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
ORDER BY (factor_id, trade_date, entity_code, params_hash);

CREATE TABLE IF NOT EXISTS ab_factor.factor_analysis_jobs
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
ORDER BY (analysis_job_id);

CREATE TABLE IF NOT EXISTS ab_factor.factor_analysis_summary
(
    analysis_job_id String,
    metric String,
    period String,
    value Nullable(Float64),
    payload_json String,
    updated_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (analysis_job_id, metric, period);

CREATE TABLE IF NOT EXISTS ab_factor.factor_analysis_ic_daily
(
    analysis_job_id String,
    trade_date Date,
    period String,
    ic Nullable(Float64),
    updated_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (analysis_job_id, period, trade_date);

CREATE TABLE IF NOT EXISTS ab_factor.factor_analysis_quantile_returns
(
    analysis_job_id String,
    trade_date Date,
    period String,
    quantile UInt8,
    mean_return Nullable(Float64),
    updated_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (analysis_job_id, period, quantile, trade_date);

CREATE TABLE IF NOT EXISTS ab_factor.factor_analysis_turnover_daily
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
ORDER BY (analysis_job_id, period, quantile, trade_date);
