from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from typing import Any
from uuid import uuid4

import requests

from factor_service.research.errors import RetryableJobError

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENTITY_ASSET_QUERY_LIMIT = 10_000
ENTITY_ASSET_QUERY_MAX_ATTEMPTS = 4
ENTITY_ASSET_QUERY_RETRY_BASE_SECONDS = 0.5
_RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_RETRYABLE_ERROR_CODES = {
    "data_unavailable",
    "indicator_backend_unavailable",
    "network_error",
    "provider_unavailable",
    "query_timeout",
    "service_unavailable",
}
_RETRYABLE_ERROR_MARKERS = (
    "connection broken",
    "connection reset",
    "entity asset extension query failed",
    "incomplete read",
    "read timed out",
    "service unavailable",
    "temporarily unavailable",
    "timeout",
    "timed out",
)


@dataclass(frozen=True)
class FactorSourceBinding:
    database: str
    table: str
    code_column: str
    date_column: str
    source_vintage: str
    date_start: date
    date_end: date
    managed_stage: bool = False


@contextmanager
def staged_entity_asset_source(
    *,
    db_client: Any,
    database: str,
    api_base_url: str,
    timeout_seconds: float,
    concurrency: int,
    entity_id: str,
    fields: Sequence[str],
    trading_dates: Sequence[date],
    date_start: date,
    date_end: date,
    job_id: str,
    data_cutoff: str = "",
    stage_key: str = "",
) -> Iterator[FactorSourceBinding]:
    """Materialize one factor's authorized composite daily asset input.

    The unified AlphaBlocks data gateway owns provider resolution and PIT-safe
    association. FactorService stages only the identity columns and fields used
    by this formula, then keeps the existing ClickHouse formula compiler and
    post-processing pipeline unchanged.
    """

    base_url = str(api_base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError(
            "因子源缺少字段，且未配置entity_asset_api_base_url，"
            "无法读取股票实体资产复合日频视图"
        )
    clean_database = _identifier(database, "factor database")
    clean_entity = str(entity_id or "").strip()
    if clean_entity != "stock":
        raise ValueError(f"实体资产复合日频暂存当前只支持stock，实际为{clean_entity or '-'}")
    clean_fields = list(
        dict.fromkeys(
            _identifier(str(field).strip(), "factor field")
            for field in fields
            if str(field).strip() not in {"code", "date", "trade_time"}
        )
    )
    if not clean_fields:
        raise ValueError("实体资产复合日频暂存缺少公式字段")
    clean_dates = sorted(set(trading_dates))
    if not clean_dates:
        raise ValueError("股票日线源在公式回看区间内没有交易日")

    remote_binding = None
    if str(stage_key or "").strip() and str(data_cutoff or "").strip():
        remote_binding = _materialize_range_stage(
            db_client=db_client,
            database=clean_database,
            api_base_url=base_url,
            timeout_seconds=timeout_seconds,
            entity_id=clean_entity,
            fields=clean_fields,
            trading_dates=clean_dates,
            date_start=date_start,
            date_end=date_end,
            stage_key=str(stage_key).strip(),
            data_cutoff=str(data_cutoff).strip(),
        )
    if remote_binding is not None:
        yield remote_binding
        return

    identity = json.dumps(
        {
            "job_id": str(job_id or ""),
            "entity_id": clean_entity,
            "fields": clean_fields,
            "date_start": clean_dates[0].isoformat(),
            "date_end": clean_dates[-1].isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    invocation_key = f"{identity}:{uuid4().hex}"
    table = (
        "factor_entity_asset_stage_"
        f"{sha256(invocation_key.encode()).hexdigest()[:20]}"
    )
    qualified_table = f"{clean_database}.{table}"
    field_columns = ",\n        ".join(
        f"{field} Nullable(Float64)" for field in clean_fields
    )
    db_client.command(f"DROP TABLE IF EXISTS {qualified_table}")
    db_client.command(
        f"""
        CREATE TABLE {qualified_table}
        (
            trade_time Date,
            code String,
            {field_columns}
        )
        ENGINE = MergeTree
        ORDER BY (code, trade_time)
        """
    )
    provenance_rows: list[dict[str, Any]] = []
    try:
        batch_size = max(1, int(concurrency or 1))
        for offset in range(0, len(clean_dates), batch_size):
            date_batch = clean_dates[offset : offset + batch_size]
            with ThreadPoolExecutor(max_workers=batch_size) as executor:
                results = list(
                    executor.map(
                        lambda item: _fetch_daily_asset_rows(
                            api_base_url=base_url,
                            timeout_seconds=timeout_seconds,
                            entity_id=clean_entity,
                            fields=clean_fields,
                            trade_date=item,
                            data_cutoff=data_cutoff,
                        ),
                        date_batch,
                    )
                )
            for rows, provenance in results:
                provenance_rows.append(provenance)
                if rows:
                    db_client.insert(
                        qualified_table,
                        rows,
                        column_names=["trade_time", "code", *clean_fields],
                    )
        source_vintage = _source_vintage(
            entity_id=clean_entity,
            date_start=clean_dates[0],
            date_end=clean_dates[-1],
            provenance_rows=provenance_rows,
        )
        yield FactorSourceBinding(
            database=clean_database,
            table=table,
            code_column="code",
            date_column="trade_time",
            source_vintage=source_vintage,
            date_start=date_start,
            date_end=date_end,
            managed_stage=False,
        )
    finally:
        db_client.command(f"DROP TABLE IF EXISTS {qualified_table}")


def _fetch_daily_asset_rows(
    *,
    api_base_url: str,
    timeout_seconds: float,
    entity_id: str,
    fields: Sequence[str],
    trade_date: date,
    data_cutoff: str = "",
) -> tuple[list[list[Any]], dict[str, Any]]:
    projection = ["code", "date", *fields]
    payload = {
        "source_kind": "asset",
        "source_id": entity_id,
        "query": {
            "sdk_contract_version": "2",
            "version": "1",
            "projection": [
                {"kind": "field", "field": field}
                for field in projection
            ],
            "all_public_fields": False,
            "filter": None,
            "group_by": [],
            "having": None,
            "order_by": [],
            "limit": ENTITY_ASSET_QUERY_LIMIT,
            "limit_explicit": True,
            "distinct": False,
        },
        "params": {
            "view": "daily",
            "date": trade_date.isoformat(),
        },
    }
    if str(data_cutoff or "").strip():
        payload["data_cutoff"] = str(data_cutoff).strip()
    body = _query_daily_asset_payload(
        api_base_url=api_base_url,
        payload=payload,
        timeout_seconds=timeout_seconds,
        trade_date=trade_date,
    )
    columns = [str(item) for item in body.get("columns", ()) or ()]
    missing = [field for field in projection if field not in columns]
    if missing:
        raise ValueError(
            "股票实体资产复合日频查询缺少字段: " + ", ".join(missing)
        )
    positions = {field: columns.index(field) for field in projection}
    staged_rows: list[list[Any]] = []
    for raw in body.get("rows", ()) or ():
        code = str(raw[positions["code"]] or "").strip()
        raw_date_value = raw[positions["date"]] or trade_date.isoformat()
        try:
            raw_date = date.fromisoformat(str(raw_date_value)[:10])
        except ValueError as exc:
            raise ValueError(
                f"股票实体资产复合日频查询返回了非法日期{raw_date_value!r}"
            ) from exc
        if not code:
            continue
        values = [
            _numeric_formula_value(raw[positions[field]], field=field)
            for field in fields
        ]
        staged_rows.append([raw_date, code, *values])
    provenance = body.get("provenance")
    return staged_rows, dict(provenance) if isinstance(provenance, dict) else {}


def _materialize_range_stage(
    *,
    db_client: Any,
    database: str,
    api_base_url: str,
    timeout_seconds: float,
    entity_id: str,
    fields: Sequence[str],
    trading_dates: Sequence[date],
    date_start: date,
    date_end: date,
    stage_key: str,
    data_cutoff: str,
) -> FactorSourceBinding | None:
    payload: dict[str, Any] = {
        "entity_id": entity_id,
        "fields": list(fields),
        "trading_dates": [item.isoformat() for item in trading_dates],
        "stage_key": stage_key,
    }
    if data_cutoff:
        payload["data_cutoff"] = data_cutoff
    try:
        response = requests.post(
            f"{api_base_url}/internal/entity-asset-stages",
            json=payload,
            timeout=max(1800.0, float(timeout_seconds or 120)),
        )
    except requests.RequestException as exc:
        raise RetryableJobError(
            f"实体资产日期范围暂存请求失败: {exc}"
        ) from exc
    if int(response.status_code) in {404, 405, 501}:
        return None
    try:
        body = response.json()
    except ValueError as exc:
        raise RetryableJobError(
            "实体资产日期范围暂存返回了非JSON响应"
        ) from exc
    if int(response.status_code) >= 400 or not isinstance(body, Mapping) or not body.get("ok"):
        error = body.get("error") if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping) else {}
        message = str(error.get("message") or body.get("message") or "暂存失败") if isinstance(body, Mapping) else "暂存失败"
        if _retryable_query_response(int(response.status_code), error, message):
            raise RetryableJobError(
                f"实体资产日期范围暂存失败: {message}"
            )
        raise ValueError(f"实体资产日期范围暂存失败: {message}")
    binding = body.get("binding")
    if not isinstance(binding, Mapping):
        raise ValueError("实体资产日期范围暂存缺少binding")
    binding_database = _identifier(
        str(binding.get("database") or ""), "stage database",
    )
    table = _identifier(str(binding.get("table") or ""), "stage table")
    if binding_database != database:
        raise ValueError(
            f"实体资产暂存数据库不一致: {binding_database} != {database}"
        )
    exists = db_client.query(
        "SELECT count() FROM system.tables "
        "WHERE database = {database:String} AND name = {table:String}",
        parameters={"database": binding_database, "table": table},
    ).result_rows[0][0]
    if int(exists or 0) != 1:
        raise RetryableJobError(
            f"实体资产日期范围暂存表不可见: {binding_database}.{table}"
        )
    return FactorSourceBinding(
        database=binding_database,
        table=table,
        code_column=_identifier(
            str(binding.get("code_column") or "code"), "stage code column",
        ),
        date_column=_identifier(
            str(binding.get("date_column") or "trade_time"), "stage date column",
        ),
        source_vintage=str(binding.get("source_vintage") or ""),
        date_start=date_start,
        date_end=date_end,
        managed_stage=True,
    )


def _query_daily_asset_payload(
    *,
    api_base_url: str,
    payload: Mapping[str, Any],
    timeout_seconds: float,
    trade_date: date,
) -> Mapping[str, Any]:
    last_failure = "查询失败"
    for attempt in range(1, ENTITY_ASSET_QUERY_MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                f"{api_base_url}/query",
                json=payload,
                timeout=max(1.0, float(timeout_seconds or 120)),
            )
        except requests.RequestException as exc:
            last_failure = str(exc) or type(exc).__name__
            if attempt >= ENTITY_ASSET_QUERY_MAX_ATTEMPTS:
                raise RetryableJobError(
                    _daily_query_failure_message(trade_date, last_failure)
                ) from exc
            _wait_before_retry(attempt)
            continue

        status_code = int(response.status_code)
        try:
            body = response.json()
        except ValueError as exc:
            last_failure = f"返回了非JSON响应({status_code})"
            if (
                status_code in _RETRYABLE_HTTP_STATUSES
                and attempt < ENTITY_ASSET_QUERY_MAX_ATTEMPTS
            ):
                _wait_before_retry(attempt)
                continue
            error_type = (
                RetryableJobError
                if status_code in _RETRYABLE_HTTP_STATUSES
                else ValueError
            )
            raise error_type(
                _daily_query_failure_message(trade_date, last_failure)
            ) from exc
        if not isinstance(body, Mapping):
            raise TypeError(
                _daily_query_failure_message(trade_date, "返回JSON不是对象")
            )
        if status_code < 400 and bool(body.get("ok")):
            return body

        error = body.get("error") if isinstance(body.get("error"), Mapping) else {}
        last_failure = str(
            error.get("message") or body.get("message") or "查询失败"
        )
        if _retryable_query_response(status_code, error, last_failure):
            if attempt < ENTITY_ASSET_QUERY_MAX_ATTEMPTS:
                _wait_before_retry(attempt)
                continue
            raise RetryableJobError(
                _daily_query_failure_message(trade_date, last_failure)
            )
        raise ValueError(
            _daily_query_failure_message(trade_date, last_failure)
        )
    raise RetryableJobError(
        _daily_query_failure_message(trade_date, last_failure)
    )


def _retryable_query_response(
    status_code: int,
    error: Mapping[str, Any],
    message: str,
) -> bool:
    if status_code in _RETRYABLE_HTTP_STATUSES or bool(error.get("retryable")):
        return True
    code = str(error.get("code") or "").strip().lower()
    if code in _RETRYABLE_ERROR_CODES:
        return True
    lowered = str(message or "").strip().lower()
    return any(marker in lowered for marker in _RETRYABLE_ERROR_MARKERS)


def _wait_before_retry(attempt: int) -> None:
    time.sleep(ENTITY_ASSET_QUERY_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))


def _daily_query_failure_message(trade_date: date, message: str) -> str:
    return (
        "股票实体资产复合日频查询失败"
        f"({trade_date.isoformat()}): {message}"
    )


def _numeric_formula_value(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"字段{field}包含非数值{value!r}，当前数值公式不能计算该字段"
        ) from exc
    return parsed if math.isfinite(parsed) else None


def _source_vintage(
    *,
    entity_id: str,
    date_start: date,
    date_end: date,
    provenance_rows: Sequence[dict[str, Any]],
) -> str:
    data_versions = sorted(
        {
            str(item.get("data_version") or "")
            for item in provenance_rows
            if str(item.get("data_version") or "")
        }
    )
    schema_versions = sorted(
        {
            str(item.get("schema_version") or "")
            for item in provenance_rows
            if str(item.get("schema_version") or "")
        }
    )
    provider_nodes = sorted(
        {
            str(node)
            for item in provenance_rows
            for node in item.get("provider_nodes", ()) or ()
            if str(node)
        }
    )
    fingerprint = sha256(
        json.dumps(
            {
                "data_versions": data_versions,
                "schema_versions": schema_versions,
                "provider_nodes": provider_nodes,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:16]
    return (
        f"entity-asset:{entity_id}/daily@{date_start.isoformat()}"
        f":{date_end.isoformat()}#{fingerprint}"
    )


def _identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.match(str(value or "")):
        raise ValueError(f"{label}不是合法标识: {value}")
    return str(value)


__all__ = [
    "FactorSourceBinding",
    "staged_entity_asset_source",
]
