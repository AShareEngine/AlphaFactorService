from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    stock_basic_table: str
    stock_basic_type_column: str
    stock_basic_stock_type_value: str


def load_settings() -> Settings:
    _load_dotenv()
    return Settings(
        host=_env("AB_FACTOR_HOST", "127.0.0.1"),
        port=int(_env("AB_FACTOR_PORT", "8100")),
        cors_origins=tuple(
            item.strip()
            for item in _env("AB_FACTOR_CORS_ORIGINS", "*").split(",")
            if item.strip()
        ),
        clickhouse_host=_env("AB_FACTOR_CLICKHOUSE_HOST", "127.0.0.1"),
        clickhouse_port=int(_env("AB_FACTOR_CLICKHOUSE_PORT", "8123")),
        clickhouse_user=_env("AB_FACTOR_CLICKHOUSE_USER", "default"),
        clickhouse_password=_env("AB_FACTOR_CLICKHOUSE_PASSWORD", ""),
        clickhouse_database=_env("AB_FACTOR_CLICKHOUSE_DATABASE", DEFAULT_FACTOR_DATABASE),
        clickhouse_secure=_env_bool("AB_FACTOR_CLICKHOUSE_SECURE", False),
        source_database=_env("AB_FACTOR_SOURCE_DATABASE", "baostock"),
        stock_daily_table=_env("AB_FACTOR_STOCK_DAILY_TABLE", "stock_daily_real"),
        stock_code_column=_env("AB_FACTOR_STOCK_CODE_COLUMN", "code"),
        stock_date_column=_env("AB_FACTOR_STOCK_DATE_COLUMN", "trade_time"),
        stock_basic_table=_env("AB_FACTOR_STOCK_BASIC_TABLE", "bs_stock_basic"),
        stock_basic_type_column=_env("AB_FACTOR_STOCK_BASIC_TYPE_COLUMN", "type"),
        stock_basic_stock_type_value=_env("AB_FACTOR_STOCK_BASIC_STOCK_TYPE_VALUE", "1"),
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
