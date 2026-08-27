from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import requests

from factor_service.research.config import Settings


DATA_SDK_QUERY_LIMIT = 200_000
DAILY_QUERY_CHUNK_DAYS = 20
INDUSTRY_TARGET_ROWS_PER_REQUEST = 120_000
INSTRUMENT_FILTER_LIMIT = 2_000
MASTER_QUERY_START = "1990-01-01"


def load_bound_trading_calendar(
    settings: Settings,
    binding: Mapping[str, Any],
    date_start: str,
    date_end: str,
) -> pd.DatetimeIndex:
    frame, _ = _query_binding(
        settings=settings,
        binding=binding,
        roles=("trade_date",),
        date_start=date_start,
        date_end_exclusive=_exclusive_end(date_end),
        distinct=True,
    )
    dates = pd.to_datetime(frame.get("trade_date"), errors="coerce")
    return pd.DatetimeIndex(dates.dropna().drop_duplicates().sort_values())


def load_bound_security_master(
    settings: Settings,
    binding: Mapping[str, Any],
    date_start: str,
    date_end: str,
) -> pd.DataFrame:
    pattern = str(binding.get("stock_type_pattern") or "STOCK").strip()
    filters = (
        [("security_type", "contains", pattern)] if pattern else []
    )
    frame, provenance = _query_binding(
        settings=settings,
        binding=binding,
        roles=(
            "instrument", "security_type", "listing_date",
            "delisting_date", "exchange",
        ),
        date_start=date_start,
        date_end_exclusive=_exclusive_end(date_end),
        filters=filters,
        distinct=True,
    )
    for column in ("listing_date", "delisting_date"):
        if column in frame:
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
    if "instrument" in frame:
        frame["instrument"] = frame["instrument"].astype(str)
    if pattern and "security_type" in frame:
        frame = frame.loc[
            frame["security_type"].astype(str).str.contains(
                pattern, case=False, regex=False, na=False,
            )
        ]
    frame = frame.dropna(subset=["instrument", "listing_date"])
    frame = frame.drop_duplicates(
        ["instrument", "listing_date", "delisting_date"], keep="last",
    )
    frame.attrs["training_data_binding"] = _binding_provenance(
        binding, [provenance],
    )
    return frame.reset_index(drop=True)


def load_bound_index_membership(
    settings: Settings,
    binding: Mapping[str, Any],
    *,
    index_code: str,
    date_end: str,
) -> pd.DataFrame:
    frame, provenance = _query_binding(
        settings=settings,
        binding=binding,
        roles=("index_code", "instrument", "in_date", "out_date"),
        date_start=MASTER_QUERY_START,
        date_end_exclusive=_exclusive_end(date_end),
        filters=[("index_code", "eq", index_code)],
        distinct=True,
    )
    for column in ("in_date", "out_date"):
        if column in frame:
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
    if "instrument" in frame:
        frame["instrument"] = frame["instrument"].astype(str)
    frame = frame.dropna(subset=["instrument", "in_date"])
    frame = frame.loc[frame["index_code"].astype(str) == str(index_code)]
    frame = frame.drop_duplicates(
        ["index_code", "instrument", "in_date", "out_date"], keep="last",
    )
    frame.attrs["training_data_binding"] = _binding_provenance(
        binding, [provenance],
    )
    return frame.reset_index(drop=True)


def load_bound_stock_daily(
    settings: Settings,
    binding: Mapping[str, Any],
    instruments: Sequence[str],
    date_start: str,
    date_end: str,
) -> pd.DataFrame:
    roles = ("trade_date", "instrument", "adjusted_close")
    frame = _query_daily_chunks(
        settings=settings,
        binding=binding,
        roles=roles,
        instruments=instruments,
        date_start=date_start,
        date_end=date_end,
    )
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["instrument"] = frame["instrument"].astype(str)
    frame["adjusted_close"] = pd.to_numeric(
        frame["adjusted_close"], errors="coerce",
    )
    frame = frame.dropna(subset=["trade_date", "instrument", "adjusted_close"])
    frame = frame.loc[frame["adjusted_close"] > 0]
    return frame.drop_duplicates(
        ["trade_date", "instrument"], keep="last",
    ).sort_values(["trade_date", "instrument"], ignore_index=True)


def load_bound_stock_status(
    settings: Settings,
    binding: Mapping[str, Any],
    observations: pd.DataFrame,
) -> pd.DataFrame:
    empty = pd.DataFrame(columns=[
        "trade_date", "instrument", "is_st", "is_delisting",
        "is_suspended",
    ])
    expected = _expected_observations(observations)
    if expected.empty:
        return empty
    frame = _query_daily_chunks(
        settings=settings,
        binding=binding,
        roles=(
            "trade_date", "instrument", "is_st", "is_delisting",
            "is_suspended",
        ),
        instruments=sorted(expected["instrument"].unique()),
        date_start=expected["trade_date"].min().date().isoformat(),
        date_end=expected["trade_date"].max().date().isoformat(),
    )
    if frame.empty:
        return empty
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["instrument"] = frame["instrument"].astype(str)
    for column in ("is_st", "is_delisting", "is_suspended"):
        if column in frame:
            frame[column] = frame[column].map(_truthy).astype(np.uint8)
        else:
            frame[column] = np.uint8(0)
    frame = expected.merge(
        frame, on=["trade_date", "instrument"], how="inner",
    )
    return frame.drop_duplicates(
        ["trade_date", "instrument"], keep="last",
    ).reset_index(drop=True)


def load_bound_industry_membership(
    settings: Settings,
    observations: pd.DataFrame,
    binding: Mapping[str, Any],
) -> pd.DataFrame:
    """Read a frozen `/database` industry binding through the unified Data SDK."""
    empty = pd.DataFrame(columns=[
        "trade_date", "instrument", "industry_entity",
        "industry_name", "industry_weight",
    ])
    expected = _expected_observations(observations)
    if expected.empty:
        return empty
    chunks = _observation_chunks(expected)
    fields = dict(binding.get("field_bindings") or {})

    def fetch(chunk: pd.DataFrame):
        filters: list[tuple[str, str, Any]] = []
        instruments = sorted(chunk["instrument"].unique())
        if len(instruments) <= INSTRUMENT_FILTER_LIMIT:
            filters.append(("instrument", "in", instruments))
        if str(fields.get("industry_level") or "").strip():
            filters.append((
                "industry_level", "eq",
                _typed_filter_value(binding.get("industry_level_value") or "1"),
            ))
        return _query_binding(
            settings=settings,
            binding=binding,
            roles=(
                "trade_date", "instrument", "industry_code",
                "industry_name", "industry_level", "weight",
            ),
            date_start=chunk["trade_date"].min().date().isoformat(),
            date_end_exclusive=(
                chunk["trade_date"].max().date() + timedelta(days=1)
            ).isoformat(),
            filters=filters,
        )

    results = _parallel(settings, chunks, fetch)
    raw = _combine_query_results(binding, results)
    if raw.empty:
        raise ValueError("设置中心绑定的行业归属节点没有返回数据")
    frame = raw.rename(columns={
        "industry_code": "industry_entity",
        "weight": "industry_weight",
    })
    if "industry_name" not in frame:
        frame["industry_name"] = ""
    if "industry_weight" not in frame:
        frame["industry_weight"] = 1.0
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["instrument"] = frame["instrument"].astype(str)
    frame["industry_entity"] = frame["industry_entity"].astype(str)
    frame["industry_weight"] = pd.to_numeric(
        frame["industry_weight"], errors="coerce",
    )
    if str(fields.get("weight") or "").strip():
        frame = frame.loc[frame["industry_weight"].fillna(0) > 0]
    frame = expected.merge(
        frame[[
            "trade_date", "instrument", "industry_entity",
            "industry_name", "industry_weight",
        ]],
        on=["trade_date", "instrument"], how="inner",
    )
    frame.attrs["training_data_binding"] = raw.attrs.get(
        "training_data_binding", {},
    )
    return frame


def _query_daily_chunks(
    *,
    settings: Settings,
    binding: Mapping[str, Any],
    roles: Sequence[str],
    instruments: Sequence[str],
    date_start: str,
    date_end: str,
) -> pd.DataFrame:
    codes = sorted({str(item) for item in instruments if str(item)})
    chunks = _date_chunks(date_start, date_end)

    def fetch(chunk: tuple[str, str]):
        filters: list[tuple[str, str, Any]] = []
        if codes and len(codes) <= INSTRUMENT_FILTER_LIMIT:
            filters.append(("instrument", "in", codes))
        return _query_binding(
            settings=settings,
            binding=binding,
            roles=roles,
            date_start=chunk[0],
            date_end_exclusive=chunk[1],
            filters=filters,
        )

    results = _parallel(settings, chunks, fetch)
    frame = _combine_query_results(binding, results)
    if frame.empty or not codes:
        return frame
    return frame.loc[frame["instrument"].astype(str).isin(codes)].copy()


def _query_binding(
    *,
    settings: Settings,
    binding: Mapping[str, Any],
    roles: Sequence[str],
    date_start: str,
    date_end_exclusive: str,
    filters: Sequence[tuple[str, str, Any]] = (),
    distinct: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    base_url = str(settings.data_sdk_api_base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("未配置模型训练读取/database节点所需的Data SDK地址")
    provider_node_id = str(binding.get("provider_node_id") or "").strip()
    if not provider_node_id:
        raise ValueError("模型训练数据绑定缺少provider_node_id")
    fields = binding.get("field_bindings")
    if not isinstance(fields, Mapping):
        raise ValueError("模型训练数据绑定缺少field_bindings")
    selected_roles = [
        role for role in roles if str(fields.get(role) or "").strip()
    ]
    projection = list(dict.fromkeys(
        str(fields[role]) for role in selected_roles
    ))
    if not projection:
        raise ValueError(f"数据节点{provider_node_id}没有可查询的映射字段")
    predicates: list[dict[str, Any]] = []
    for role, operator, value in filters:
        field = str(fields.get(role) or "").strip()
        if not field:
            continue
        predicates.append({
            "kind": "condition",
            "field": field,
            "op": operator,
            "value": value,
            "options": {},
        })
    predicate: dict[str, Any] | None
    if not predicates:
        predicate = None
    elif len(predicates) == 1:
        predicate = predicates[0]
    else:
        predicate = {"kind": "logic", "op": "and", "items": predicates}
    payload = {
        "source_kind": "node",
        "source_id": provider_node_id,
        "query": {
            "sdk_contract_version": "2",
            "version": "1",
            "projection": [
                {"kind": "field", "field": field} for field in projection
            ],
            "all_public_fields": False,
            "filter": predicate,
            "group_by": [],
            "having": None,
            "order_by": [],
            "limit": DATA_SDK_QUERY_LIMIT,
            "limit_explicit": True,
            "distinct": distinct,
        },
        "params": {"start": date_start, "end": date_end_exclusive},
    }
    try:
        response = requests.post(
            f"{base_url}/query",
            json=payload,
            timeout=max(1.0, float(settings.data_sdk_query_timeout_seconds or 120)),
        )
    except requests.RequestException as exc:
        raise ValueError(f"数据节点{provider_node_id}查询失败: {exc}") from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise ValueError(f"数据节点{provider_node_id}返回了非JSON响应") from exc
    if response.status_code >= 400 or body.get("ok") is not True:
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        message = str(error.get("message") or body.get("message") or "查询失败")
        raise ValueError(f"数据节点{provider_node_id}查询失败: {message}")
    columns = [str(item) for item in body.get("columns", ()) or ()]
    missing = [field for field in projection if field not in columns]
    if missing:
        raise ValueError("数据节点返回缺少字段: " + ", ".join(missing))
    raw_rows: Sequence[Sequence[Any]] = body.get("rows", ()) or ()
    if len(raw_rows) >= DATA_SDK_QUERY_LIMIT:
        raise ValueError(
            f"数据节点{provider_node_id}单批返回达到"
            f"{DATA_SDK_QUERY_LIMIT}行上限，请缩小查询区间"
        )
    source_frame = pd.DataFrame(raw_rows, columns=columns)
    canonical = pd.DataFrame(index=source_frame.index)
    for role in selected_roles:
        canonical[role] = source_frame[str(fields[role])]
    provenance = body.get("provenance")
    return canonical, dict(provenance) if isinstance(provenance, dict) else {}


def _combine_query_results(
    binding: Mapping[str, Any],
    results: Sequence[tuple[pd.DataFrame, dict[str, Any]]],
) -> pd.DataFrame:
    frames = [frame for frame, _ in results if not frame.empty]
    result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    result.attrs["training_data_binding"] = _binding_provenance(
        binding, [provenance for _, provenance in results],
    )
    return result


def _binding_provenance(
    binding: Mapping[str, Any],
    provenance: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "binding_id": str(binding.get("binding_id") or ""),
        "settings_revision": int(binding.get("settings_revision") or 0),
        "fingerprint": str(binding.get("fingerprint") or ""),
        "source_type": str(binding.get("source_type") or ""),
        "source_id": str(binding.get("source_id") or ""),
        "provider_node_id": str(binding.get("provider_node_id") or ""),
        "query_count": len(provenance),
        "data_versions": sorted({
            str(item.get("data_version") or "")
            for item in provenance if str(item.get("data_version") or "")
        }),
        "schema_versions": sorted({
            str(item.get("schema_version") or "")
            for item in provenance if str(item.get("schema_version") or "")
        }),
    }


def _parallel(settings: Settings, items: Sequence[Any], function):
    concurrency = max(
        1,
        min(int(settings.data_sdk_query_concurrency or 1), len(items) or 1),
    )
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        return list(executor.map(function, items))


def _date_chunks(date_start: str, date_end: str) -> list[tuple[str, str]]:
    start = pd.Timestamp(date_start).date()
    final = pd.Timestamp(date_end).date()
    chunks: list[tuple[str, str]] = []
    while start <= final:
        inclusive_end = min(
            start + timedelta(days=DAILY_QUERY_CHUNK_DAYS - 1), final,
        )
        chunks.append((
            start.isoformat(),
            (inclusive_end + timedelta(days=1)).isoformat(),
        ))
        start = inclusive_end + timedelta(days=1)
    return chunks


def _observation_chunks(frame: pd.DataFrame) -> list[pd.DataFrame]:
    chunks: list[pd.DataFrame] = []
    pending: list[pd.DataFrame] = []
    pending_rows = 0
    for _date, group in frame.groupby("trade_date", sort=True):
        daily = group[["trade_date", "instrument"]].drop_duplicates()
        if (
            pending
            and pending_rows + len(daily) > INDUSTRY_TARGET_ROWS_PER_REQUEST
        ):
            chunks.append(pd.concat(pending, ignore_index=True))
            pending = []
            pending_rows = 0
        pending.append(daily)
        pending_rows += len(daily)
    if pending:
        chunks.append(pd.concat(pending, ignore_index=True))
    return chunks


def _expected_observations(observations: pd.DataFrame) -> pd.DataFrame:
    if observations.empty:
        return pd.DataFrame(columns=["trade_date", "instrument"])
    expected = observations[["trade_date", "instrument"]].drop_duplicates().copy()
    expected["trade_date"] = pd.to_datetime(expected["trade_date"], errors="coerce")
    expected["instrument"] = expected["instrument"].astype(str)
    return expected.dropna(subset=["trade_date"])


def _exclusive_end(value: str | date) -> str:
    return (pd.Timestamp(value).date() + timedelta(days=1)).isoformat()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value) != 0.0
    return str(value or "").strip().lower() in {
        "1", "true", "t", "yes", "y", "是",
    }


def _typed_filter_value(value: Any) -> Any:
    text = str(value or "").strip()
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


__all__ = [
    "load_bound_index_membership",
    "load_bound_industry_membership",
    "load_bound_security_master",
    "load_bound_stock_daily",
    "load_bound_stock_status",
    "load_bound_trading_calendar",
]
