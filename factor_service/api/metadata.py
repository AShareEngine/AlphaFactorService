from __future__ import annotations

import re
from threading import Lock
from time import monotonic

from fastapi import APIRouter, HTTPException, Query

from factor_service.clickhouse import client, settings


router = APIRouter(prefix="/metadata", tags=["metadata"])
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SOURCE_RANGE_CACHE_SECONDS = 60.0
_source_range_cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
_source_range_lock = Lock()


@router.get("/source-range")
def source_range(entity_type: str = Query(default="stock")) -> dict:
    if entity_type != "stock":
        raise HTTPException(status_code=400, detail=f"暂不支持的实体类型: {entity_type}")

    config = settings()
    source_database = _identifier(config.source_database, "source database")
    source_table = _identifier(config.stock_daily_table, "stock daily table")
    date_column = _identifier(config.stock_date_column, "stock date column")

    cache_key = (source_database, source_table, date_column)
    with _source_range_lock:
        cached = _source_range_cache.get(cache_key)
        now = monotonic()
        if cached and now - cached[0] < SOURCE_RANGE_CACHE_SECONDS:
            return dict(cached[1])

        rows = client().query(
            f"""
            SELECT min({date_column}) AS date_start, max({date_column}) AS date_end
            FROM {source_database}.{source_table}
            """
        ).result_rows
        row = rows[0] if rows else (None, None)
        payload = {
            "entity_type": entity_type,
            "source_database": source_database,
            "source_table": source_table,
            "date_column": date_column,
            "date_start": row[0],
            "date_end": row[1],
        }
        _source_range_cache[cache_key] = (now, payload)
        return dict(payload)


def _identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.match(value or ""):
        raise HTTPException(status_code=500, detail=f"{label} 不是合法标识: {value}")
    return value
