from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


OutputType = Literal["number", "boolean", "category", "rank"]
Frequency = Literal["daily", "event", "financial", "intraday"]
JobMode = Literal["incremental", "backfill", "recompute"]
JobStatus = Literal["pending", "running", "success", "failed", "cancelled"]


class FactorBase(BaseModel):
    factor_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str = ""
    entity_type: str = "stock"
    category: str = "custom"
    group_name: str = "custom"
    output_type: OutputType = "number"
    frequency: Frequency = "daily"
    required_fields: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
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
    required_fields: Optional[list[str]] = None
    params: Optional[dict[str, Any]] = None
    expression: Optional[str] = None
    enabled: Optional[bool] = None


class FactorOut(FactorBase):
    version: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


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
    updated_at: Optional[datetime] = None


class CoverageOut(BaseModel):
    factor_id: str
    date_start: Optional[date] = None
    date_end: Optional[date] = None
    rows: int
    entity_count: int
    trade_date_count: int
