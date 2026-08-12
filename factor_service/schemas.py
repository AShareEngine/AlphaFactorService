from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


OutputType = Literal["number", "boolean", "category", "rank"]
Frequency = Literal["daily", "minute"]
JobMode = Literal["incremental", "backfill", "recompute"]
JobStatus = Literal["pending", "running", "success", "failed", "cancelled"]
AnalysisStatus = Literal["pending", "running", "success", "failed", "cancelled"]


class FactorBase(BaseModel):
    factor_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str = ""
    entity_type: str = "stock"
    category: str = "custom"
    group_name: str = "custom"
    output_type: OutputType = "number"
    frequency: Frequency = "daily"
    asset_id: str = ""
    source_node_id: str = ""
    required_fields: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    param_schema: dict[str, dict[str, Any]] = Field(default_factory=dict)
    availability_policy: dict[str, Any] = Field(
        default_factory=lambda: {
            "field": "available_at",
            "policy": "persisted_timestamp",
        }
    )
    expression: str = ""
    enabled: bool = True


class FactorCreate(FactorBase):
    pass


class FactorUpdate(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    entity_type: Optional[str] = None
    category: Optional[str] = None
    group_name: Optional[str] = None
    output_type: Optional[OutputType] = None
    frequency: Optional[Frequency] = None
    asset_id: Optional[str] = None
    source_node_id: Optional[str] = None
    required_fields: Optional[list[str]] = None
    params: Optional[dict[str, Any]] = None
    param_schema: Optional[dict[str, dict[str, Any]]] = None
    availability_policy: Optional[dict[str, Any]] = None
    expression: Optional[str] = None
    enabled: Optional[bool] = None


class FactorOut(FactorBase):
    version: int
    available_versions: list[int] = Field(default_factory=list)
    definition_hash: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class FactorFormulaValidateRequest(BaseModel):
    expression: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    code_column: str = "code"
    date_column: str = "trade_date"


class FactorFormulaValidateOut(BaseModel):
    valid: bool
    expression: str
    required_fields: list[str] = Field(default_factory=list)
    max_window: int = 1
    compiled_sql: str = ""
    error_message: str = ""


class FactorJobCreate(BaseModel):
    factor_id: str = Field(min_length=1)
    factor_version: Optional[int] = None
    entity_type: str = "stock"
    mode: JobMode = "incremental"
    universe: str = "current_pool"
    date_start: Optional[date] = None
    date_end: Optional[date] = None
    params: dict[str, Any] = Field(default_factory=dict)


class FactorJobOut(BaseModel):
    job_id: str
    factor_id: str
    factor_version: int
    entity_type: str
    mode: JobMode
    universe: str
    date_start: Optional[date] = None
    date_end: Optional[date] = None
    params: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus
    error_message: str = ""
    row_count: Optional[int] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class FactorValueOut(BaseModel):
    trade_date: date
    entity_type: str
    entity_code: str
    factor_id: str
    factor_version: int
    params_hash: str
    raw_value: Optional[float] = None
    rank_value: Optional[int] = None
    percentile: Optional[float] = None
    score: Optional[float] = None
    job_id: str
    available_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CoverageOut(BaseModel):
    factor_id: str
    date_start: Optional[date] = None
    date_end: Optional[date] = None
    rows: int
    entity_count: int
    trade_date_count: int


class FactorValueMetricQualityOut(BaseModel):
    field: str
    rows: int
    count: int
    null_count: int
    zero_count: int
    null_ratio: float = 0
    zero_ratio: float = 0
    min: Optional[float] = None
    max: Optional[float] = None
    avg: Optional[float] = None
    stddev: Optional[float] = None
    all_null: bool = False
    all_zero: bool = False


class FactorValueQualityOut(BaseModel):
    factor_id: str
    factor_version: Optional[int] = None
    entity_type: Optional[str] = None
    params_hash: str = ""
    job_id: str = ""
    date_start: Optional[date] = None
    date_end: Optional[date] = None
    rows: int
    entity_count: int
    trade_date_count: int
    latest_updated_at: Optional[datetime] = None
    metrics: dict[str, FactorValueMetricQualityOut] = Field(default_factory=dict)
    postprocess_status: str = "unknown"
    warnings: list[str] = Field(default_factory=list)


class FactorAnalysisJobCreate(BaseModel):
    factor_id: str = Field(min_length=1)
    factor_version: Optional[int] = None
    entity_type: str = "stock"
    params_hash: str = ""
    date_start: Optional[date] = None
    date_end: Optional[date] = None
    periods: list[int] = Field(default_factory=lambda: [1, 5, 10])
    quantiles: int = Field(default=5, ge=2, le=20)
    price_field: str = "close"
    cumulative_returns: bool = True
    max_loss: float = Field(default=0.9, ge=0, le=1)


class FactorAnalysisJobOut(BaseModel):
    analysis_job_id: str
    factor_id: str
    factor_version: int
    entity_type: str
    params_hash: str = ""
    date_start: Optional[date] = None
    date_end: Optional[date] = None
    periods: list[int] = Field(default_factory=list)
    quantiles: int
    price_field: str
    cumulative_returns: bool
    max_loss: float
    status: AnalysisStatus
    error_message: str = ""
    row_count: Optional[int] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class FactorAnalysisSummaryOut(BaseModel):
    analysis_job_id: str
    metric: str
    period: str = ""
    value: Optional[float] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    updated_at: Optional[datetime] = None


class FactorAnalysisIcOut(BaseModel):
    analysis_job_id: str
    trade_date: date
    period: str
    ic: Optional[float] = None
    updated_at: Optional[datetime] = None


class FactorAnalysisQuantileReturnOut(BaseModel):
    analysis_job_id: str
    trade_date: date
    period: str
    quantile: int
    mean_return: Optional[float] = None
    updated_at: Optional[datetime] = None


class FactorAnalysisTurnoverOut(BaseModel):
    analysis_job_id: str
    trade_date: date
    period: str
    quantile: int
    turnover: Optional[float] = None
    rank_autocorrelation: Optional[float] = None
    updated_at: Optional[datetime] = None
