from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
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


def load_bound_registered_membership(
    settings: Settings,
    source: Mapping[str, Any],
    calendar: pd.DatetimeIndex,
    *,
    date_start: str,
    date_end: str,
    data_cutoff: str,
) -> pd.DataFrame:
    """Materialize one immutable registered membership asset on a calendar."""

    binding = source.get("binding")
    if not isinstance(binding, Mapping):
        raise ValueError("自定义股票池缺少冻结数据节点绑定")
    shape = str(source.get("membership_shape") or "")
    if shape == "daily_snapshot":
        frame = _query_daily_chunks(
            settings=settings,
            binding=binding,
            roles=("trade_date", "instrument"),
            instruments=(),
            date_start=date_start,
            date_end=date_end,
            data_cutoff=data_cutoff,
        )
        provenance = dict(frame.attrs.get("training_data_binding") or {})
        if not frame.empty:
            frame["trade_date"] = pd.to_datetime(
                frame["trade_date"], errors="coerce",
            )
            frame["instrument"] = frame["instrument"].astype(str)
            frame = frame.dropna(subset=["trade_date", "instrument"])
            allowed = pd.DataFrame({"trade_date": calendar})
            frame = frame.merge(allowed, on="trade_date", how="inner")
            frame = frame[["trade_date", "instrument"]].drop_duplicates()
    elif shape == "interval":
        intervals, query_provenance = _query_binding(
            settings=settings,
            binding=binding,
            roles=("instrument", "in_date", "out_date"),
            date_start=MASTER_QUERY_START,
            date_end_exclusive=_exclusive_end(date_end),
            distinct=True,
            data_cutoff=data_cutoff,
        )
        provenance = _binding_provenance(binding, [query_provenance])
        for column in ("in_date", "out_date"):
            intervals[column] = pd.to_datetime(
                intervals[column], errors="coerce",
            )
        intervals["instrument"] = intervals["instrument"].astype(str)
        intervals = intervals.dropna(
            subset=["instrument", "in_date"],
        ).drop_duplicates(
            ["instrument", "in_date", "out_date"], keep="last",
        )
        intervals["out_date"] = intervals["out_date"].fillna(
            pd.Timestamp(date_end),
        )
        frame = _expand_registered_intervals(calendar, intervals)
    else:
        raise ValueError("自定义股票池成员形态无效")
    frame = frame.sort_values(
        ["trade_date", "instrument"], ignore_index=True,
    )
    frame.attrs["training_data_binding"] = {
        **provenance,
        "registered_membership_source": {
            "source_id": str(source.get("source_id") or ""),
            "asset_id": str(source.get("asset_id") or ""),
            "asset_version": int(source.get("asset_version") or 0),
            "asset_version_id": str(source.get("asset_version_id") or ""),
            "asset_source_hash": str(source.get("asset_source_hash") or ""),
            "binding_fingerprint": str(
                source.get("binding_fingerprint") or ""
            ),
            "membership_shape": shape,
            "data_cutoff": data_cutoff,
        },
    }
    return frame


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


def load_bound_universe_filter_membership(
    settings: Settings,
    binding: Mapping[str, Any],
    observations: pd.DataFrame,
    *,
    operator: str,
    value: Any = None,
    data_type: str = "",
    data_cutoff: str = "",
) -> pd.DataFrame:
    """Return observation keys satisfying one frozen entity-field predicate."""

    expected = _expected_observations(observations)
    if expected.empty:
        return expected
    instruments = sorted(expected["instrument"].unique())
    frame = _query_daily_chunks(
        settings=settings,
        binding=binding,
        roles=("trade_date", "instrument", "value"),
        instruments=instruments,
        date_start=expected["trade_date"].min().date().isoformat(),
        date_end=expected["trade_date"].max().date().isoformat(),
        data_cutoff=data_cutoff,
    )
    if frame.empty:
        result = expected.iloc[0:0].copy()
    else:
        frame["trade_date"] = pd.to_datetime(
            frame["trade_date"], errors="coerce",
        )
        frame["instrument"] = frame["instrument"].astype(str)
        matches = _universe_filter_mask(
            frame["value"], operator=operator, value=value,
            data_type=data_type,
        )
        matched = frame.loc[
            matches, ["trade_date", "instrument"]
        ].dropna().drop_duplicates()
        result = expected.merge(
            matched, on=["trade_date", "instrument"], how="inner",
        )
    result.attrs["training_data_binding"] = dict(
        frame.attrs.get("training_data_binding") or {}
    )
    return result.sort_values(
        ["trade_date", "instrument"], ignore_index=True,
    )


def _universe_filter_mask(
    source: pd.Series,
    *,
    operator: str,
    value: Any,
    data_type: str,
) -> pd.Series:
    """Evaluate a public, type-normalized field predicate fail-closed."""

    clean_operator = str(operator or "").strip().lower()
    series, normalized_value = _normalized_comparison_values(
        source, value=value, data_type=data_type,
    )
    present = series.notna()
    if clean_operator == "is_null":
        return series.isna()
    if clean_operator == "not_null":
        return present
    if clean_operator == "eq":
        mask = series.eq(normalized_value)
    elif clean_operator == "ne":
        mask = present & series.ne(normalized_value)
    elif clean_operator == "gt":
        mask = series.gt(normalized_value)
    elif clean_operator == "gte":
        mask = series.ge(normalized_value)
    elif clean_operator == "lt":
        mask = series.lt(normalized_value)
    elif clean_operator == "lte":
        mask = series.le(normalized_value)
    elif clean_operator == "in":
        mask = series.isin(normalized_value)
    elif clean_operator == "not_in":
        mask = present & ~series.isin(normalized_value)
    elif clean_operator == "between":
        mask = series.between(
            normalized_value[0], normalized_value[1], inclusive="both",
        )
    elif clean_operator in {"contains", "starts_with", "ends_with"}:
        text = series.astype("string")
        pattern = str(normalized_value)
        if clean_operator == "contains":
            mask = text.str.contains(pattern, case=True, regex=False, na=False)
        elif clean_operator == "starts_with":
            mask = text.str.startswith(pattern, na=False)
        else:
            mask = text.str.endswith(pattern, na=False)
    else:
        raise ValueError(f"实体资产股票池字段运算符不受支持: {clean_operator}")
    return pd.Series(mask, index=series.index).fillna(False).astype(bool)


def _normalized_comparison_values(
    source: pd.Series, *, value: Any, data_type: str,
) -> tuple[pd.Series, Any]:
    clean_type = str(data_type or "").strip().lower()
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    if "bool" in clean_type:
        series = source.map(_nullable_boolean).astype("boolean")
        normalized = [
            _nullable_boolean(item) for item in values
        ]
    elif any(token in clean_type for token in (
        "int", "float", "double", "decimal", "number",
    )):
        series = pd.to_numeric(source, errors="coerce")
        normalized = [pd.to_numeric(item, errors="coerce") for item in values]
    elif any(token in clean_type for token in ("date", "time")):
        series = pd.to_datetime(source, errors="coerce", utc=True)
        normalized = [
            pd.to_datetime(item, errors="coerce", utc=True) for item in values
        ]
    else:
        series = source.astype("string")
        normalized = [str(item) for item in values]
    return series, normalized if isinstance(value, (list, tuple)) else normalized[0]


def _nullable_boolean(value: Any) -> Any:
    if value is None or pd.isna(value):
        return pd.NA
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float)):
        if float(value) == 1.0:
            return True
        if float(value) == 0.0:
            return False
        return pd.NA
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "是"}:
        return True
    if text in {"0", "false", "f", "no", "n", "否", ""}:
        return False
    return pd.NA


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
    data_cutoff: str = "",
) -> pd.DataFrame:
    codes = sorted({str(item) for item in instruments if str(item)})
    chunks = _date_chunks(date_start, date_end)

    def fetch(chunk: tuple[str, str]):
        chunk_filters: list[tuple[str, str, Any]] = []
        if codes and len(codes) <= INSTRUMENT_FILTER_LIMIT:
            chunk_filters.append(("instrument", "in", codes))
        return _query_binding(
            settings=settings,
            binding=binding,
            roles=roles,
            date_start=chunk[0],
            date_end_exclusive=chunk[1],
            filters=chunk_filters,
            data_cutoff=data_cutoff,
        )

    results = _parallel(settings, chunks, fetch)
    frame = _combine_query_results(binding, results)
    if frame.empty or not codes:
        return frame
    return frame.loc[frame["instrument"].astype(str).isin(codes)].copy()


def _expand_registered_intervals(
    calendar: pd.DatetimeIndex,
    intervals: pd.DataFrame,
) -> pd.DataFrame:
    if len(calendar) == 0 or intervals.empty:
        return pd.DataFrame(columns=["trade_date", "instrument"])
    dates = pd.DataFrame({"trade_date": calendar})
    parts: list[pd.DataFrame] = []
    for row in intervals.itertuples(index=False):
        active = dates.loc[
            dates["trade_date"].between(row.in_date, row.out_date)
        ].copy()
        if active.empty:
            continue
        active["instrument"] = str(row.instrument)
        parts.append(active)
    if not parts:
        return pd.DataFrame(columns=["trade_date", "instrument"])
    return pd.concat(parts, ignore_index=True).drop_duplicates()


def _query_binding(
    *,
    settings: Settings,
    binding: Mapping[str, Any],
    roles: Sequence[str],
    date_start: str,
    date_end_exclusive: str,
    filters: Sequence[tuple[str, str, Any]] = (),
    distinct: bool = False,
    data_cutoff: str = "",
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
        predicate = {
            "kind": "condition",
            "field": field,
            "op": operator,
            "options": {},
        }
        if operator not in {"is_null", "not_null"}:
            predicate["value"] = value
        predicates.append(predicate)
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
    if data_cutoff:
        payload["data_cutoff"] = str(data_cutoff)
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
    provenance = dict(provenance) if isinstance(provenance, dict) else {}
    _assert_frozen_provider_version(binding, provenance)
    if data_cutoff and not _same_instant(
        provenance.get("data_cutoff"), data_cutoff,
    ):
        raise ValueError(
            f"数据节点{provider_node_id}返回的data_cutoff与冻结计划不一致"
        )
    return canonical, provenance


def _same_instant(left: Any, right: Any) -> bool:
    try:
        left_time = datetime.fromisoformat(
            str(left or "").strip().replace("Z", "+00:00")
        )
        right_time = datetime.fromisoformat(
            str(right or "").strip().replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if left_time.tzinfo is None or right_time.tzinfo is None:
        return False
    return left_time == right_time


def _assert_frozen_provider_version(
    binding: Mapping[str, Any], provenance: Mapping[str, Any],
) -> None:
    expected_version_id = str(
        binding.get("provider_node_version_id") or ""
    ).strip()
    if not expected_version_id:
        return
    expected = {
        "version": int(binding.get("provider_node_version") or 0),
        "version_id": expected_version_id,
        "source_hash": str(
            binding.get("provider_node_source_hash") or ""
        ).strip().lower(),
    }
    actual = {
        "version": int(provenance.get("source_registry_version") or 0),
        "version_id": str(
            provenance.get("source_registry_version_id") or ""
        ).strip(),
        "source_hash": str(
            provenance.get("source_registry_hash") or ""
        ).strip().lower(),
    }
    if actual != expected:
        raise ValueError(
            "自定义股票池数据节点已变更；冻结版本与当前Data SDK来源不一致"
        )


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
    result = {
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
    data_cutoffs = sorted({
        str(item.get("data_cutoff") or "")
        for item in provenance if str(item.get("data_cutoff") or "")
    })
    if data_cutoffs:
        result["data_cutoffs"] = data_cutoffs
    return result


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
        # These temporary request keys must not inherit DataFrame.attrs from
        # the upstream factor/price frame. Pandas compares attrs while
        # concatenating; array-valued provenance makes that comparison raise
        # "truth value of an array is ambiguous" before the query can run.
        daily.attrs = {}
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
    "load_bound_registered_membership",
    "load_bound_industry_membership",
    "load_bound_security_master",
    "load_bound_stock_daily",
    "load_bound_stock_status",
    "load_bound_universe_filter_membership",
    "load_bound_trading_calendar",
]
