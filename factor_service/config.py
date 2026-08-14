from __future__ import annotations

from dataclasses import dataclass

from factor_service.runtime_config import (
    as_bool,
    load_runtime_config,
    section,
    string_list,
)


DEFAULT_FACTOR_DATABASE = "ab_factor"


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
    clickhouse_secure: bool
    source_database: str
    stock_daily_table: str
    stock_code_column: str
    stock_date_column: str
    stock_price_column: str
    stock_basic_table: str
    stock_basic_type_column: str
    stock_basic_stock_type_value: str
    model_database: str
    research_internal_url: str


def load_settings() -> Settings:
    runtime = load_runtime_config()
    service = section(runtime, "service")
    clickhouse = section(runtime, "clickhouse")
    source = section(runtime, "sources", "factor")
    return Settings(
        host=str(service.get("host") or "127.0.0.1").strip(),
        port=int(service.get("port") or 8100),
        cors_origins=string_list(service.get("cors_origins"), ("*",)),
        clickhouse_host=str(clickhouse.get("host") or "127.0.0.1").strip(),
        clickhouse_port=int(clickhouse.get("port") or 8123),
        clickhouse_user=str(clickhouse.get("username") or "default").strip(),
        clickhouse_password=str(clickhouse.get("password") or ""),
        clickhouse_database=str(
            clickhouse.get("factor_database") or DEFAULT_FACTOR_DATABASE
        ).strip(),
        clickhouse_secure=as_bool(clickhouse.get("secure"), False),
        source_database=str(source.get("database") or "baostock").strip(),
        stock_daily_table=str(source.get("stock_daily_table") or "stock_daily_real").strip(),
        stock_code_column=str(source.get("stock_code_column") or "code").strip(),
        stock_date_column=str(source.get("stock_date_column") or "trade_time").strip(),
        stock_price_column=str(source.get("stock_price_column") or "close").strip(),
        stock_basic_table=str(source.get("stock_basic_table") or "bs_stock_basic").strip(),
        stock_basic_type_column=str(source.get("stock_basic_type_column") or "type").strip(),
        stock_basic_stock_type_value=str(
            source.get("stock_basic_stock_type_value") or "1"
        ).strip(),
        model_database=str(clickhouse.get("model_database") or "ab_model").strip(),
        research_internal_url=str(
            service.get("research_internal_url") or "http://127.0.0.1:8787"
        ).strip().rstrip("/"),
    )
