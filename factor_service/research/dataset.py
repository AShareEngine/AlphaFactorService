from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
import math
import re
from typing import Any
from zoneinfo import ZoneInfo

import clickhouse_connect
import numpy as np
import pandas as pd

from factor_service import repository as factor_repository
from factor_service.config import load_settings as load_factor_settings
from factor_service.entity_asset_source import staged_entity_asset_source
from factor_service.entity_field_feature import (
    is_entity_field_feature,
    virtual_entity_field_factor,
)
from factor_service.factor_backtest import UNIVERSES
from factor_service.qlib_formula import compile_qlib_formula
from factor_service.research.config import Settings
from factor_service.research.job import CancellationToken, ProgressCallback
from factor_service.research.industry_feature import (
    INDUSTRY_FEATURE_SAFE_START,
    append_industry_one_hot_features,
    normalize_industry_feature,
)
from factor_service.research.data_binding_source import (
    load_bound_index_membership,
    load_bound_industry_membership,
    load_bound_registered_membership,
    load_bound_security_master,
    load_bound_size_rotation_daily,
    load_bound_stock_daily,
    load_bound_stock_status,
    load_bound_trading_calendar,
    load_bound_universe_filter_membership,
)
from factor_service.research.preprocessing import (
    LEGACY_DATASET_PIPELINE_VERSIONS,
    normalize_feature_preprocessing,
    preprocess_feature_panel,
)
from factor_service.research.size_rotation_feature import (
    append_size_rotation_features,
    normalize_size_rotation_feature,
    size_rotation_lookback_sessions,
)
from factor_service.research.sample_filter_formula import (
    compile_sample_filter_formula,
    normalize_custom_sample_filters,
)
from factor_service.research.training_resource_settings import (
    INDEX_MEMBERSHIP_BINDING_ID,
    INDUSTRY_FEATURE_BINDING_ID,
    SECURITY_MASTER_BINDING_ID,
    STOCK_DAILY_BINDING_ID,
    STOCK_STATUS_BINDING_ID,
    TRADING_CALENDAR_BINDING_ID,
    frozen_data_binding,
    get_training_resource_settings,
    normalize_frozen_training_data_bindings,
    training_data_binding,
    training_data_binding_ready,
)
from factor_service.research.universe_field_filter import (
    normalize_universe_field_filters,
)
from factor_service.schemas import FactorOut
from factor_service.worker import build_factor_query_plan, factor_query_source


FACTOR_COMPUTED_CUTOFF = "computed_at <= {cutoff:DateTime}"
FACTOR_EVENT_CUTOFF = (
    "event_available_at <= toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR"
)
SW2021_INDUSTRY_SAFE_START = INDUSTRY_FEATURE_SAFE_START
DEFAULT_FACTOR_QUERY_CHUNK_DAYS = 90
LISTING_AGE_CALENDAR_CODE = "000001.SH"
SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DATASET_SPLIT_RESOLUTION_SCHEMA_VERSION = (
    "alphablocks.dataset-split-resolution.v1"
)


@dataclass(frozen=True)
class PreparedDataset:
    frame: pd.DataFrame
    segments: dict[str, tuple[str, str]]
    feature_names: list[str]
    coverage: dict[str, float]
    medians: dict[str, float]
    manifest: dict[str, Any]
    raw_frame: pd.DataFrame | None = None


class DatasetBuilder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.factor_query_chunk_days = max(
            30,
            min(
                int(
                    getattr(
                        settings,
                        "factor_query_chunk_days",
                        DEFAULT_FACTOR_QUERY_CHUNK_DAYS,
                    )
                ),
                366,
            ),
        )
        self.client = clickhouse_connect.get_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            autogenerate_session_id=False,
        )

    def check(self) -> dict[str, Any]:
        version = self.client.query("SELECT version()").result_rows[0][0]
        factors = self.client.query(
            f"SELECT count() FROM {self.settings.factor_database}.factor_definitions FINAL WHERE enabled = 1"
        ).result_rows[0][0]
        sentinel = self.audit_future_function_sentinel()
        return {
            "clickhouse_version": version,
            "enabled_factor_count": int(factors),
            "future_function_sentinel": sentinel,
            "training_targets": self.target_capabilities(),
        }

    def target_capabilities(self) -> list[dict[str, Any]]:
        try:
            configured = get_training_resource_settings()
            configured_industry_binding = training_data_binding(
                configured, INDUSTRY_FEATURE_BINDING_ID,
            )
            industry_feature_ready = training_data_binding_ready(
                configured_industry_binding, INDUSTRY_FEATURE_BINDING_ID,
            ) and bool(self.settings.data_sdk_api_base_url)
            industry_feature_reason = (
                "行业One-hot已绑定/database中的"
                f"{configured_industry_binding.get('source_label') or configured_industry_binding.get('source_id')}。"
                if industry_feature_ready
                else "请先在设置中心为行业One-hot绑定/database数据节点。"
            )
        except Exception:
            industry_feature_ready = False
            industry_feature_reason = (
                "请先在设置中心为行业One-hot绑定/database数据节点。"
            )
        return [
            {
                "target": "stock_selection",
                "label": "个股选股",
                "ready": True,
                "prediction_scope": "stock",
                "supports_industry_feature": True,
                "industry_feature_ready": industry_feature_ready,
                "industry_feature_minimum_date": INDUSTRY_FEATURE_SAFE_START,
                "industry_feature_reason": industry_feature_reason,
                "reason": "支持冻结T+1至T+30个股收益截面排名或涨跌方向标签。",
                "missing_fields": [],
            },
            {
                "target": "industry_rotation",
                "label": "申万一级行业轮动",
                "ready": industry_feature_ready,
                "prediction_scope": "industry",
                "supports_industry_feature": False,
                "industry_feature_ready": False,
                "reason": (
                    "使用申万2021版日频行业权重；仅允许从2021-12-13起训练，禁止使用更早的回溯重分类。"
                    if industry_feature_ready
                    else "请先在设置中心绑定行业归属。"
                ),
                "missing_fields": [] if industry_feature_ready else [
                    INDUSTRY_FEATURE_BINDING_ID,
                ],
                "minimum_date": SW2021_INDUSTRY_SAFE_START,
                "source_contract": "sw2021_daily_weight_snapshot",
            },
        ]

    def audit_future_function_sentinel(self) -> dict[str, Any]:
        """Execute the production PIT predicates against safe and future rows."""
        visible = self.client.query(
            f"""
            SELECT groupArray(sample_id)
            FROM (
                SELECT 'safe' AS sample_id, toDate('2024-01-02') AS trade_date,
                       toDateTime('2024-01-02 14:00:00', 'Asia/Shanghai') AS computed_at,
                       toDateTime('2024-01-02 14:30:00', 'Asia/Shanghai') AS event_available_at
                UNION ALL
                SELECT 'future_computed', toDate('2024-01-02'),
                       toDateTime('2024-01-02 16:00:00', 'Asia/Shanghai'),
                       toDateTime('2024-01-02 14:30:00', 'Asia/Shanghai')
                UNION ALL
                SELECT 'future_event', toDate('2024-01-02'),
                       toDateTime('2024-01-02 14:00:00', 'Asia/Shanghai'),
                       toDateTime('2024-01-02 16:00:00', 'Asia/Shanghai')
            )
            WHERE {FACTOR_COMPUTED_CUTOFF}
              AND {FACTOR_EVENT_CUTOFF}
            """,
            parameters={"cutoff": datetime(2024, 1, 2, 15, 0)},
        ).result_rows[0][0]
        visible_rows = sorted(str(item) for item in visible)
        if visible_rows != ["safe"]:
            raise ValueError("未来函数哨兵检查失败: " + ", ".join(visible_rows))
        return {
            "ok": True,
            "visible_rows": visible_rows,
            "excluded_rows": ["future_computed", "future_event"],
        }

    def build(
        self,
        job: dict[str, Any],
        *,
        cancellation: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
    ) -> PreparedDataset:
        spec = dict(job.get("dataset_spec") or (job.get("config_json") or {}).get("dataset") or {})
        data_bindings = normalize_frozen_training_data_bindings(
            spec.get("data_bindings"), allow_empty=True,
        )
        factors = list(spec["factors"])
        cutoff = datetime.fromisoformat(str(spec["data_cutoff"]).replace("Z", "+00:00"))
        if cutoff.tzinfo is not None:
            cutoff_for_clickhouse = cutoff.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        else:
            cutoff_for_clickhouse = cutoff
        date_start = str(spec["date_start"])
        date_end = str(spec["date_end"])
        signal_close = datetime.combine(
            pd.Timestamp(date_end).date(), datetime.min.time(),
        ).replace(hour=15)
        if cutoff_for_clickhouse < signal_close:
            raise ValueError("data_cutoff不得早于训练结束日收盘时间")
        feature_frames: list[pd.DataFrame] = []
        feature_names: list[str] = []
        preprocessing_excluded_features: list[str] = []
        coverage: dict[str, float] = {}
        preprocessing = normalize_feature_preprocessing(
            spec.get("preprocessing"), default_enabled=False,
        )
        industry_feature = normalize_industry_feature(
            spec.get("industry_feature"), default_enabled=False,
        )
        industry_feature_details: dict[str, Any] = {
            "feature_names": [], "mapped_coverage": None,
        }
        size_rotation_feature = normalize_size_rotation_feature(
            spec.get("size_rotation_feature"), default_enabled=False,
        )
        size_rotation_feature_details: dict[str, Any] = {
            "feature_names": [], "coverage": {}, "signal_date_count": 0,
        }
        legacy_labeled_panel_medians = (
            spec.get("preprocessing") is None
            and str(spec.get("pipeline_version") or "")
            in LEGACY_DATASET_PIPELINE_VERSIONS
        )
        _checkpoint(cancellation)
        _progress(progress, "building_membership", 6, {})
        universe_id = str(spec.get("universe_id") or "csi500")
        index_code = str(spec.get("index_code") or "000905.SH")
        universe_field_filters = normalize_universe_field_filters(
            spec.get("universe_field_filters")
        )
        membership = self._membership(
            date_start, date_end, universe_id=universe_id, index_code=index_code,
            sample_filters=spec.get("sample_filters"),
            universe_field_filters=universe_field_filters,
            data_bindings=data_bindings,
            universe_source=spec.get("universe_source"),
            data_cutoff=str(spec["data_cutoff"]),
        )
        if membership.empty:
            raise ValueError("历史股票池应用样本过滤后为空")
        membership_calendar_value = membership.attrs.get("_trading_calendar")
        membership_calendar = pd.DatetimeIndex(
            [] if membership_calendar_value is None else membership_calendar_value
        )
        universe_filter_steps = list(
            membership.attrs.get("universe_filter_steps") or []
        )
        universe_source_provenance = dict(
            membership.attrs.get("training_data_binding") or {}
        )
        expected = membership[["trade_date", "instrument"]].drop_duplicates()
        expected_count = max(1, len(expected))
        minimum_coverage = float(
            spec.get("minimum_factor_coverage") or 0.8
        )
        resolved_factors = [self._factor_definition(item) for item in factors]
        entity_asset_groups: dict[str, list[tuple[dict[str, Any], FactorOut]]] = {}
        for item, factor in zip(factors, resolved_factors, strict=True):
            group_key = _entity_asset_batch_key(factor)
            if group_key:
                entity_asset_groups.setdefault(group_key, []).append((item, factor))
        batched_frames: dict[tuple[str, str], pd.DataFrame] = {}
        loaded_groups: set[str] = set()
        for index, item in enumerate(factors, start=1):
            _checkpoint(cancellation)
            name = str(item["factor_id"])
            _progress(progress, "loading_factors", 8 + int(26 * (index - 1) / len(factors)), {
                "factor_id": name, "factor_index": index, "factor_count": len(factors),
            })
            factor = resolved_factors[index - 1]
            group_key = _entity_asset_batch_key(factor)
            group = entity_asset_groups.get(group_key, [])
            frame_key = (name, str(item.get("params_hash") or ""))
            if group_key and len(group) > 1:
                if group_key not in loaded_groups:
                    batched_frames.update(self._factor_values_batch(
                        group,
                        cutoff_for_clickhouse,
                        date_start,
                        date_end,
                        cancellation=cancellation,
                        progress=progress,
                    ))
                    loaded_groups.add(group_key)
                frame = batched_frames[frame_key]
            else:
                frame = self._factor_values(
                    item, cutoff_for_clickhouse, date_start, date_end,
                    cancellation=cancellation,
                    resolved_factor=factor,
                )
            frame = frame.merge(expected, on=["trade_date", "instrument"], how="inner")
            actual_coverage = frame[["trade_date", "instrument"]].drop_duplicates().shape[0] / expected_count
            coverage[name] = actual_coverage
            if actual_coverage < minimum_coverage:
                raise ValueError(
                    f"因子{name}覆盖率{actual_coverage:.2%}低于"
                    f"{minimum_coverage:.0%}"
                )
            feature_name = _feature_name(item)
            feature_names.append(feature_name)
            if str(factor.output_type or "").strip().lower() in {
                "boolean", "category",
            }:
                preprocessing_excluded_features.append(feature_name)
            feature_frames.append(frame.rename(columns={"value": feature_name}))
        _checkpoint(cancellation)
        features = expected.copy()
        for frame in feature_frames:
            features = features.merge(frame, on=["trade_date", "instrument"], how="left")
        if size_rotation_feature["enabled"]:
            if str(spec.get("research_target") or "stock_selection") != "stock_selection":
                raise ValueError("大小盘轮动特征仅支持个股选股训练目标")
            features, size_rotation_feature_details = self._size_rotation_features(
                features,
                date_start=date_start,
                date_end=date_end,
                index_code=index_code,
                universe_id=universe_id,
                size_rotation_feature=size_rotation_feature,
                data_bindings=data_bindings,
                data_cutoff=str(spec["data_cutoff"]),
            )
            size_names = list(
                size_rotation_feature_details.get("feature_names") or []
            )
            size_coverages = dict(
                size_rotation_feature_details.get("coverage") or {}
            )
            low_size = [
                name for name in size_names
                if float(size_coverages.get(name) or 0.0) < minimum_coverage
            ]
            if low_size:
                raise ValueError(
                    "大小盘轮动特征覆盖率低于"
                    f"{minimum_coverage:.0%}: " + ", ".join(low_size)
                )
            feature_names.extend(size_names)
            coverage.update(size_coverages)
        _progress(progress, "loading_prices", 38, {"instrument_count": int(features["instrument"].nunique())})
        prices = self._adjusted_close(
            sorted(features["instrument"].unique()), date_start, date_end,
            data_bindings=data_bindings,
        )
        _checkpoint(cancellation)
        _progress(progress, "building_labels", 46, {})
        research_target = str(spec.get("research_target") or "stock_selection")
        if industry_feature["enabled"]:
            if research_target != "stock_selection":
                raise ValueError("行业编码特征仅支持个股选股训练目标")
            if pd.Timestamp(date_start) < pd.Timestamp(INDUSTRY_FEATURE_SAFE_START):
                raise ValueError(
                    "行业编码特征仅支持2021-12-13及以后；"
                    "更早历史包含申万2021版回溯重分类"
                )
            industry_membership = self._industry_membership(
                expected, date_start, date_end,
                industry_feature=industry_feature,
                data_bindings=data_bindings,
            )
            industry_source_details = dict(
                industry_membership.attrs.get("training_data_binding") or {}
            )
            features, industry_feature_details = (
                append_industry_one_hot_features(
                    features, industry_membership, industry_feature,
                )
            )
            if industry_source_details:
                industry_feature_details["data_binding"] = (
                    industry_source_details
                )
            mapped_coverage = float(
                industry_feature_details.get("mapped_coverage") or 0.0
            )
            if mapped_coverage < minimum_coverage:
                raise ValueError(
                    "申万一级行业One-hot映射覆盖率"
                    f"{mapped_coverage:.2%}低于{minimum_coverage:.0%}"
                )
            industry_names = list(
                industry_feature_details.get("feature_names") or []
            )
            feature_names.extend(industry_names)
            preprocessing_excluded_features.extend(industry_names)
        label_spec = dict(spec.get("label") or {})
        horizon = int(label_spec.get("horizon_trading_days") or 5)
        target_mode = str(
            spec.get("target_mode") or label_spec.get("mode") or "return"
        ).strip().lower()
        classification = target_mode == "classification"
        target_contract: dict[str, Any]
        if research_target == "industry_rotation":
            industry_membership = self._industry_membership(
                expected, date_start, date_end,
                data_bindings=data_bindings,
            )
            features, industry_membership = _industry_features(
                features, industry_membership, feature_names,
            )
            labels = _industry_rank_label(
                prices, industry_membership, horizon=horizon,
                classification=classification,
            )
            target_contract = {
                "research_target": "industry_rotation",
                "prediction_scope": "industry",
                "feature_aggregation": "daily_industry_weighted_mean",
                "membership": "sw2021_daily_weight_snapshot",
                "safe_start": SW2021_INDUSTRY_SAFE_START,
                "target_mode": target_mode,
                "label": (
                    "future_industry_return_direction"
                    if classification
                    else "future_industry_return_cross_sectional_rank"
                ),
            }
        elif research_target == "stock_selection":
            labels = (
                _future_direction_label(prices, horizon=horizon)
                if classification
                else _future_rank_label(prices, horizon=horizon)
            )
            target_contract = {
                "research_target": "stock_selection",
                "prediction_scope": "stock",
                "feature_aggregation": "none",
                "target_mode": target_mode,
                "label": (
                    "future_stock_return_direction"
                    if classification
                    else "future_stock_return_cross_sectional_rank"
                ),
            }
        else:
            raise ValueError(f"训练目标{research_target}尚不可用")
        # Labels only determine which rows can train the model.  They must not
        # determine the daily feature cross-section: whether T+N is available
        # is future information and inference sees the complete signal-date
        # universe.  Split dates are derived from trainable rows, while all
        # preprocessing statistics below are fitted on the feature universe.
        raw_panel = features.merge(
            labels, on=["trade_date", "instrument"], how="inner",
        )
        raw_panel.sort_values(["trade_date", "instrument"], inplace=True)
        split_config = dict(spec.get("split") or {})
        segments = materialized_dataset_segments(
            split=split_config,
            membership_calendar=membership_calendar,
            available_sample_dates=raw_panel["trade_date"],
            label_horizon_trading_days=horizon,
        )
        _progress(progress, "splitting_dataset", 52, {"segments": segments})
        train_start, train_end = segments["train"]
        median_source = raw_panel if legacy_labeled_panel_medians else features
        train_mask = median_source["trade_date"].between(
            pd.Timestamp(train_start), pd.Timestamp(train_end),
        )
        if classification:
            train_classes = set(
                pd.to_numeric(
                    raw_panel.loc[
                        raw_panel["trade_date"].between(
                            pd.Timestamp(train_start), pd.Timestamp(train_end),
                        ),
                        "LABEL0",
                    ],
                    errors="coerce",
                )
                .dropna()
                .astype(int)
                .unique()
                .tolist()
            )
            if train_classes != {0, 1}:
                raise ValueError("分类目标训练段必须同时包含上涨和下跌样本")
        medians = {
            name: float(
                pd.to_numeric(median_source.loc[train_mask, name], errors="coerce")
                .replace([np.inf, -np.inf], np.nan)
                .median()
            )
            for name in feature_names
        }
        if any(not np.isfinite(value) for value in medians.values()):
            missing = [name for name, value in medians.items() if not np.isfinite(value)]
            raise ValueError("训练段无法计算因子中位数: " + ", ".join(missing))
        _checkpoint(cancellation)
        processed_features = preprocess_feature_panel(
            features,
            feature_names,
            preprocessing,
            fallback_values=medians,
            excluded_features=preprocessing_excluded_features,
        )
        panel = processed_features.merge(
            labels, on=["trade_date", "instrument"], how="inner",
        )
        panel.sort_values(["trade_date", "instrument"], inplace=True)
        raw_indexed = raw_panel.set_index(["trade_date", "instrument"])
        raw_indexed.index.names = ["datetime", "instrument"]
        raw_indexed = raw_indexed[feature_names + ["LABEL0"]]
        raw_indexed.columns = pd.MultiIndex.from_tuples(
            [("feature", name) for name in feature_names] + [("label", "LABEL0")]
        )
        indexed = panel.set_index(["trade_date", "instrument"])
        indexed.index.names = ["datetime", "instrument"]
        indexed = indexed[feature_names + ["LABEL0"]]
        indexed.columns = pd.MultiIndex.from_tuples(
            [("feature", name) for name in feature_names] + [("label", "LABEL0")]
        )
        future_function_guards = [
            "factor definitions and parameters frozen before materialization",
            "source rows limited to signal date and available by market close",
            "data_cutoff >= final signal date close",
            "historical index membership",
            "five session split embargo",
        ]
        if preprocessing["enabled"]:
            future_function_guards.append(
                "same-date cross-sectional median, 1/99 winsorization and z-score only"
            )
            future_function_guards.append(
                "feature cross-sections frozen before future labels are joined"
            )
            if preprocessing_excluded_features:
                future_function_guards.append(
                    "boolean/category features excluded from cross-sectional scaling and filled by train medians"
                )
        else:
            future_function_guards.append("missing values filled by train-only medians")
            if legacy_labeled_panel_medians:
                future_function_guards.append(
                    "legacy v1-v5 train medians fitted on label-available rows for hash-compatible rebuild"
                )
        sample_filters = dict(spec.get("sample_filters") or {})
        if sample_filters:
            future_function_guards.append(
                "daily point-in-time listing age, ST and delisting filters applied before materialization"
            )
        if universe_field_filters:
            future_function_guards.append(
                "entity-asset field predicates resolved to frozen node versions and applied point-in-time"
            )
        if sample_filters.get("custom_formulas"):
            future_function_guards.append(
                "custom sample formulas use allowlisted point-in-time fields and backward-only windows"
            )
        if industry_feature["enabled"]:
            future_function_guards.extend([
                "stock industry uses exact-date SW2021 L1 snapshots no earlier than 2021-12-13",
                "frozen 31-category one-hot vocabulary plus one unknown bucket",
                "industry one-hot columns are already complete and excluded from winsorization and z-score transforms",
            ])
        if size_rotation_feature["enabled"]:
            future_function_guards.extend([
                "large and small baskets use exact-date frozen configured stock-pool membership",
                "basket returns use only signal-date and backward adjusted-close history",
                "size exposure uses same-date target-universe float-market-cap cross-sections",
                "rotation interactions share one implementation between training and inference",
            ])
        if research_target == "industry_rotation":
            future_function_guards.append(
                "industry membership uses exact-date SW2021 weight snapshots no earlier than 2021-12-13"
            )
        manifest = {
            "schema_version": "alphablocks.qlib-dataset.v1",
            "dataset_hash": str(job.get("dataset_hash") or ""),
            "row_count": len(indexed),
            "instrument_count": int(indexed.index.get_level_values("instrument").nunique()),
            "feature_names": feature_names,
            "coverage": coverage,
            "medians": medians,
            "preprocessing": preprocessing,
            "preprocessing_stage": "training_universe_after_factor_score",
            "preprocessing_compatibility": (
                "legacy_labeled_panel_train_medians"
                if legacy_labeled_panel_medians else "current"
            ),
            "preprocessing_excluded_features": sorted(
                preprocessing_excluded_features,
            ),
            "industry_feature": industry_feature,
            "size_rotation_feature": size_rotation_feature,
            "data_bindings": data_bindings,
            "industry_feature_details": industry_feature_details,
            "size_rotation_feature_details": size_rotation_feature_details,
            "segments": segments,
            "data_cutoff": cutoff.isoformat(),
            "target_contract": target_contract,
            "target_ref": dict(spec.get("target_ref") or {}),
            "transform_refs": list(spec.get("transform_refs") or []),
            "universe_rule_refs": list(
                spec.get("universe_rule_refs") or []
            ),
            "universe_field_filters": universe_field_filters,
            "sample_filters": sample_filters,
            "universe_filter_steps": universe_filter_steps,
            "universe_source": dict(spec.get("universe_source") or {}),
            "universe_source_provenance": universe_source_provenance,
            "future_function_guards": future_function_guards,
            "materialization": {
                "mode": "on_demand",
                "format": "parquet",
                "persist_factor_values": False,
            },
        }
        manifest["content_fingerprint"] = _frame_fingerprint(indexed)
        _progress(progress, "dataset_ready", 56, {
            "row_count": len(indexed), "feature_count": len(feature_names),
        })
        return PreparedDataset(
            indexed, segments, feature_names, coverage, medians, manifest,
            raw_frame=raw_indexed,
        )

    def _size_rotation_features(
        self,
        observations: pd.DataFrame,
        *,
        date_start: str,
        date_end: str,
        index_code: str,
        universe_id: str,
        size_rotation_feature: dict[str, Any],
        data_bindings: dict[str, Any] | None,
        data_cutoff: str,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        lookback = size_rotation_lookback_sessions(size_rotation_feature)
        observed_dates = pd.to_datetime(
            observations.get("trade_date"), errors="coerce",
        ).dropna()
        if observed_dates.empty:
            raise ValueError("大小盘轮动目标样本不包含有效交易日")
        first_observation_date = observed_dates.min().date().isoformat()
        history_start = self.trading_dates_ending_at(
            first_observation_date,
            lookback,
            index_code=index_code,
            universe_id=universe_id,
            data_bindings=data_bindings,
        )[0]

        def pool_membership(pool: dict[str, Any]) -> pd.DataFrame:
            selector = dict(pool["selector"])
            return self._membership(
                history_start,
                date_end,
                universe_id=str(pool["source_id"]),
                index_code=str(selector["value"]),
                sample_filters={},
                universe_field_filters=[],
                data_bindings=data_bindings,
                universe_source=pool,
                data_cutoff=data_cutoff,
            )

        large_membership = pool_membership(size_rotation_feature["large_pool"])
        small_membership = pool_membership(size_rotation_feature["small_pool"])
        instruments = sorted(set(
            observations["instrument"].astype(str)
        ).union(
            large_membership["instrument"].astype(str)
        ).union(
            small_membership["instrument"].astype(str)
        ))
        daily_binding = frozen_data_binding(
            data_bindings, STOCK_DAILY_BINDING_ID,
        )
        if daily_binding is None:
            raise ValueError("大小盘轮动特征缺少冻结的训练基础行情绑定")
        daily = load_bound_size_rotation_daily(
            self.settings,
            daily_binding,
            instruments,
            history_start,
            date_end,
            data_cutoff=data_cutoff,
        )
        result, details = append_size_rotation_features(
            observations,
            daily,
            large_membership,
            small_membership,
            size_rotation_feature,
        )
        source_details = dict(
            daily.attrs.get("training_data_binding") or {}
        )
        if source_details:
            details["data_binding"] = source_details
        details["history_start"] = history_start
        return result, details

    def _membership(
        self,
        date_start: str,
        date_end: str,
        *,
        universe_id: str = "csi500",
        index_code: str = "000905.SH",
        sample_filters: dict[str, Any] | None = None,
        universe_field_filters: list[dict[str, Any]] | None = None,
        data_bindings: dict[str, Any] | None = None,
        universe_source: dict[str, Any] | None = None,
        data_cutoff: str = "",
    ) -> pd.DataFrame:
        frozen_source = dict(universe_source or {})
        registered_source = (
            frozen_source
            if frozen_source.get("source_kind") == "entity_asset"
            else {}
        )
        configured_source = (
            frozen_source
            if frozen_source.get("source_kind") == "configured_stock_pool"
            else {}
        )
        if frozen_source and not str(data_cutoff or "").strip():
            raise ValueError("自定义历史成员股票池缺少冻结data_cutoff")
        if universe_id not in UNIVERSES and not frozen_source:
            raise ValueError(f"不支持的股票池: {universe_id}")
        if (
            not frozen_source
            and index_code not in {
                config["index_code"] for config in UNIVERSES.values()
            }
        ):
            raise ValueError(f"不支持的基准指数: {index_code}")
        calendar_binding = frozen_data_binding(
            data_bindings, TRADING_CALENDAR_BINDING_ID,
        )
        if calendar_binding is not None:
            calendar = load_bound_trading_calendar(
                self.settings, calendar_binding, date_start, date_end,
            )
            if calendar.empty:
                raise ValueError("设置中心绑定的交易日历在训练区间内为空")
            if registered_source:
                frame = load_bound_registered_membership(
                    self.settings,
                    registered_source,
                    calendar,
                    date_start=date_start,
                    date_end=date_end,
                    data_cutoff=data_cutoff,
                )
            elif universe_id == "all_a":
                master_binding = frozen_data_binding(
                    data_bindings, SECURITY_MASTER_BINDING_ID,
                )
                if master_binding is None:
                    raise ValueError("全A股票池缺少冻结的证券历史主数据绑定")
                master = load_bound_security_master(
                    self.settings, master_binding, date_start, date_end,
                )
                frame = _expand_membership_intervals(
                    calendar,
                    master.rename(columns={
                        "listing_date": "in_date",
                        "delisting_date": "out_date",
                    }),
                )
            else:
                membership_binding = frozen_data_binding(
                    data_bindings, INDEX_MEMBERSHIP_BINDING_ID,
                )
                if membership_binding is None:
                    raise ValueError("指数股票池缺少冻结的指数成分绑定")
                intervals = load_bound_index_membership(
                    self.settings, membership_binding,
                    index_code=index_code, date_end=date_end,
                )
                frame = _expand_membership_intervals(calendar, intervals)
            provenance = dict(frame.attrs.get("training_data_binding") or {})
            if not frame.empty:
                frame = self._apply_sample_filters(
                    frame,
                    date_start=date_start,
                    date_end=date_end,
                    sample_filters=sample_filters,
                    universe_field_filters=universe_field_filters,
                    data_bindings=data_bindings,
                    data_cutoff=data_cutoff,
                )
                if provenance:
                    frame.attrs["training_data_binding"] = provenance
            frame.attrs["_trading_calendar"] = pd.DatetimeIndex(calendar)
            return frame

        if registered_source or configured_source:
            raise ValueError("配置历史成员股票池必须使用冻结的交易日历绑定")

        calendar_code = "000905.SH" if universe_id == "all_a" else index_code
        if universe_id == "all_a":
            rows = self.client.query(
                f"""
                SELECT DISTINCT toDate(trade_time) AS trade_date, code AS instrument
                FROM {self.settings.source_database}.ad_market_kline_daily
                WHERE toDate(trade_time) >= {{date_start:Date}}
                  AND toDate(trade_time) <= {{date_end:Date}}
                  AND code IN (
                      SELECT code
                      FROM baostock.bs_stock_basic
                      WHERE type = '1'
                  )
                ORDER BY trade_date, instrument
                """,
                parameters={
                    "date_start": date_start, "date_end": date_end,
                },
            ).result_rows
        else:
            rows = self.client.query(
                f"""
                SELECT calendar.trade_date, members.con_code
                FROM (
                    SELECT DISTINCT toDate(trade_time) AS trade_date
                    FROM {self.settings.source_database}.ad_market_kline_daily
                    WHERE code = {{calendar_code:String}}
                      AND toDate(trade_time) >= {{date_start:Date}}
                      AND toDate(trade_time) <= {{date_end:Date}}
                ) AS calendar
                CROSS JOIN (
                    SELECT con_code, in_date, out_date
                    FROM {self.settings.source_database}.ad_index_constituent
                    WHERE index_code = {{index_code:String}}
                      AND in_date <= {{date_end:Date}}
                      AND (out_date IS NULL OR out_date >= {{date_start:Date}})
                ) AS members
                WHERE members.in_date <= calendar.trade_date
                  AND (members.out_date IS NULL OR members.out_date >= calendar.trade_date)
                ORDER BY calendar.trade_date, members.con_code
                """,
                parameters={
                    "date_start": date_start, "date_end": date_end,
                    "calendar_code": calendar_code,
                    "index_code": index_code,
                },
            ).result_rows
        frame = pd.DataFrame(rows, columns=["trade_date", "instrument"])
        if not frame.empty:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            calendar = pd.DatetimeIndex(
                frame["trade_date"].dropna().drop_duplicates().sort_values()
            )
            frame = self._apply_sample_filters(
                frame,
                date_start=date_start,
                date_end=date_end,
                sample_filters=sample_filters,
                universe_field_filters=universe_field_filters,
                data_bindings=data_bindings,
                data_cutoff=data_cutoff,
            )
            frame.attrs["_trading_calendar"] = calendar
        return frame

    def _apply_sample_filters(
        self,
        membership: pd.DataFrame,
        *,
        date_start: str,
        date_end: str,
        sample_filters: dict[str, Any] | None,
        universe_field_filters: list[dict[str, Any]] | None = None,
        data_bindings: dict[str, Any] | None = None,
        data_cutoff: str = "",
    ) -> pd.DataFrame:
        filters = dict(sample_filters or {})
        minimum_days = int(filters.get("minimum_listing_trading_days") or 0)
        exclude_st = filters.get("exclude_st") is True
        exclude_delisting = filters.get("exclude_delisting") is True
        field_filters = normalize_universe_field_filters(
            universe_field_filters
        )
        custom_formulas = normalize_custom_sample_filters(
            filters.get("custom_formulas", []),
        )
        source_provenance = dict(
            membership.attrs.get("training_data_binding") or {}
        ).get("registered_membership_source") or {}
        steps: list[dict[str, Any]] = [
            _universe_filter_step(
                "membership_source",
                len(membership),
                len(membership),
                params={
                    key: source_provenance[key]
                    for key in (
                        "asset_id", "asset_version", "asset_version_id",
                        "binding_fingerprint", "membership_shape",
                    )
                    if key in source_provenance
                },
            )
        ]
        if (
            minimum_days <= 0
            and not exclude_st
            and not exclude_delisting
            and not field_filters
            and not custom_formulas
        ):
            result = membership[
                ["trade_date", "instrument"]
            ].drop_duplicates().sort_values(
                ["trade_date", "instrument"], ignore_index=True,
            )
            result.attrs["universe_filter_steps"] = steps
            return result

        result = membership.copy()
        instruments = sorted(result["instrument"].astype(str).unique().tolist())
        if minimum_days > 0:
            before_count = len(result)
            master_binding = frozen_data_binding(
                data_bindings, SECURITY_MASTER_BINDING_ID,
            )
            calendar_binding = frozen_data_binding(
                data_bindings, TRADING_CALENDAR_BINDING_ID,
            )
            if master_binding is not None and calendar_binding is not None:
                calendar_start = (
                    pd.Timestamp(date_start)
                    - timedelta(days=minimum_days * 3 + 30)
                ).date().isoformat()
                basics = load_bound_security_master(
                    self.settings, master_binding, calendar_start, date_end,
                )[["instrument", "listing_date"]].drop_duplicates()
                calendar = load_bound_trading_calendar(
                    self.settings, calendar_binding, calendar_start, date_end,
                )
                if calendar.empty:
                    raise ValueError("缺少上市交易日过滤所需的交易日历")
                result = result.merge(basics, on="instrument", how="left")
                signal_positions = calendar.searchsorted(result["trade_date"])
                ipo_positions = calendar.searchsorted(
                    result["listing_date"].fillna(result["trade_date"]),
                )
                result["listing_trading_days"] = (
                    signal_positions - ipo_positions
                )
                result = result[
                    result["listing_trading_days"] >= minimum_days
                ]
            else:
                result = self._apply_legacy_listing_age_filter(
                    result, date_start, date_end, minimum_days, instruments,
                )
            steps.append(_universe_filter_step(
                "listing_age",
                before_count,
                len(result),
                params={"minimum_trading_days": minimum_days},
            ))

        for predicate in field_filters:
            before_count = len(result)
            result = load_bound_universe_filter_membership(
                self.settings,
                dict(predicate["binding"]),
                result,
                operator=str(predicate["operator"]),
                value=predicate.get("value"),
                data_type=str(predicate["data_type"]),
                data_cutoff=data_cutoff,
            )
            steps.append(_universe_filter_step(
                "entity_field",
                before_count,
                len(result),
                params={
                    "asset_id": predicate["asset_id"],
                    "provider_node": predicate["provider_node"],
                    "field": predicate["field"],
                    "operator": predicate["operator"],
                    **(
                        {"value": predicate["value"]}
                        if "value" in predicate else {}
                    ),
                    "missing_policy": predicate["missing_policy"],
                    "binding_fingerprint": predicate["binding"]["fingerprint"],
                },
            ))
            if result.empty:
                break
        
        if (exclude_st or exclude_delisting) and not result.empty:
            status_binding = frozen_data_binding(
                data_bindings, STOCK_STATUS_BINDING_ID,
            )
            if status_binding is not None:
                statuses = load_bound_stock_status(
                    self.settings, status_binding,
                    result[["trade_date", "instrument"]],
                )
                result = result.merge(
                    statuses,
                    on=["trade_date", "instrument"],
                    how="left",
                )
                result[["is_st", "is_delisting"]] = result[
                    ["is_st", "is_delisting"]
                ].fillna(0)
                if exclude_st:
                    before_count = len(result)
                    result = result[result["is_st"] != 1]
                    steps.append(_universe_filter_step(
                        "exclude_st", before_count, len(result),
                    ))
                if exclude_delisting:
                    before_count = len(result)
                    result = result[result["is_delisting"] != 1]
                    steps.append(_universe_filter_step(
                        "exclude_delisting", before_count, len(result),
                    ))
            else:
                if exclude_st:
                    before_count = len(result)
                    result = self._apply_legacy_status_filter(
                        result, date_start, date_end,
                        exclude_st=True,
                        exclude_delisting=False,
                    )
                    steps.append(_universe_filter_step(
                        "exclude_st", before_count, len(result),
                    ))
                if exclude_delisting and not result.empty:
                    before_count = len(result)
                    result = self._apply_legacy_status_filter(
                        result, date_start, date_end,
                        exclude_st=False,
                        exclude_delisting=True,
                    )
                    steps.append(_universe_filter_step(
                        "exclude_delisting", before_count, len(result),
                    ))

        if custom_formulas and not result.empty:
            for formula in custom_formulas:
                before_count = len(result)
                result = self._apply_custom_formula_filters(
                    result,
                    date_start=date_start,
                    date_end=date_end,
                    formulas=[formula],
                )
                steps.append(_universe_filter_step(
                    f"formula:{str(formula.get('formula_hash') or formula.get('hash') or '')[:16]}",
                    before_count,
                    len(result),
                    params={
                        "expression": str(formula.get("expression") or ""),
                    },
                ))
                if result.empty:
                    break

        output = result[["trade_date", "instrument"]].drop_duplicates().sort_values(
            ["trade_date", "instrument"], ignore_index=True,
        )
        output.attrs["universe_filter_steps"] = steps
        return output

    def _apply_legacy_listing_age_filter(
        self,
        result: pd.DataFrame,
        date_start: str,
        date_end: str,
        minimum_days: int,
        instruments: list[str],
    ) -> pd.DataFrame:
        basic_rows = self.client.query(
            """
            SELECT code, toDateOrNull(ipo_date)
            FROM baostock.bs_stock_basic
            WHERE type = '1' AND code IN {codes:Array(String)}
            """,
            parameters={"codes": instruments},
        ).result_rows
        basics = pd.DataFrame(
            basic_rows, columns=["instrument", "ipo_date"],
        )
        basics["ipo_date"] = pd.to_datetime(
            basics["ipo_date"], errors="coerce",
        )
        calendar_start = (
            pd.Timestamp(date_start)
            - timedelta(days=minimum_days * 3 + 30)
        ).date().isoformat()
        calendar_rows = self.client.query(
            f"""
            SELECT DISTINCT toDate(trade_time) AS trade_date
            FROM {self.settings.source_database}.ad_market_kline_daily
            WHERE code = {{calendar_code:String}}
              AND toDate(trade_time) >= {{calendar_start:Date}}
              AND toDate(trade_time) <= {{date_end:Date}}
            ORDER BY trade_date
            """,
            parameters={
                "calendar_code": LISTING_AGE_CALENDAR_CODE,
                "calendar_start": calendar_start,
                "date_end": date_end,
            },
        ).result_rows
        calendar = pd.DatetimeIndex(
            pd.to_datetime([row[0] for row in calendar_rows]),
        )
        if calendar.empty:
            raise ValueError("缺少上市交易日过滤所需的A股交易日历")
        result = result.merge(basics, on="instrument", how="left")
        signal_positions = calendar.searchsorted(result["trade_date"])
        ipo_positions = calendar.searchsorted(
            result["ipo_date"].fillna(result["trade_date"]),
        )
        result["listing_trading_days"] = signal_positions - ipo_positions
        return result[result["listing_trading_days"] >= minimum_days]

    def _apply_legacy_status_filter(
        self,
        result: pd.DataFrame,
        date_start: str,
        date_end: str,
        *,
        exclude_st: bool,
        exclude_delisting: bool,
    ) -> pd.DataFrame:
        result = result.drop(
            columns=["is_st", "is_delisting"], errors="ignore",
        )
        status_rows = self.client.query(
                f"""
                SELECT
                    toDate(trade_date) AS trade_date,
                    market_code AS instrument,
                    toUInt8(ifNull(is_st_sec, '') IN ('1','true','True')) AS is_st,
                    toUInt8(ifNull(is_wd_sec, '') IN ('1','true','True')) AS is_delisting
                FROM {self.settings.source_database}.ad_history_stock_status
                WHERE market_code IN {{codes:Array(String)}}
                  AND toDate(trade_date) >= {{date_start:Date}}
                  AND toDate(trade_date) <= {{date_end:Date}}
                """,
                parameters={
                    "codes": sorted(
                        result["instrument"].astype(str).unique().tolist()
                    ),
                    "date_start": date_start,
                    "date_end": date_end,
                },
        ).result_rows
        statuses = pd.DataFrame(
            status_rows,
            columns=["trade_date", "instrument", "is_st", "is_delisting"],
        )
        if not statuses.empty:
            statuses["trade_date"] = pd.to_datetime(statuses["trade_date"])
            statuses = statuses.drop_duplicates(
                ["trade_date", "instrument"], keep="last",
            )
            result = result.merge(
                statuses, on=["trade_date", "instrument"], how="left",
            )
        else:
            result["is_st"] = 0
            result["is_delisting"] = 0
        result[["is_st", "is_delisting"]] = result[
            ["is_st", "is_delisting"]
        ].fillna(0)
        if exclude_st:
            result = result[result["is_st"] != 1]
        if exclude_delisting:
            result = result[result["is_delisting"] != 1]
        return result

    def _apply_custom_formula_filters(
        self,
        membership: pd.DataFrame,
        *,
        date_start: str,
        date_end: str,
        formulas: list[dict[str, Any]],
    ) -> pd.DataFrame:
        entity_asset_formulas = [
            item for item in formulas if item.get("field_bindings")
        ]
        legacy_formulas = [
            item for item in formulas if not item.get("field_bindings")
        ]
        result = membership
        if legacy_formulas:
            result = self._apply_legacy_custom_formula_filters(
                result,
                date_start=date_start,
                date_end=date_end,
                formulas=legacy_formulas,
            )
        if entity_asset_formulas and not result.empty:
            result = self._apply_entity_asset_formula_filters(
                result,
                date_start=date_start,
                date_end=date_end,
                formulas=entity_asset_formulas,
            )
        return result

    def _apply_entity_asset_formula_filters(
        self,
        membership: pd.DataFrame,
        *,
        date_start: str,
        date_end: str,
        formulas: list[dict[str, Any]],
    ) -> pd.DataFrame:
        combined_expression = " && ".join(
            f"({str(item['expression'])})" for item in formulas
        )
        required_fields = sorted({
            str(field)
            for item in formulas
            for field in item.get("required_fields", [])
        })
        identity = sha256(
            json.dumps(
                formulas,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        factor = FactorOut(
            factor_id=f"sample_filter_group_{identity[:20]}",
            label="训练股票池自定义筛选",
            description="使用冻结的股票实体资产字段按交易日筛选训练与推理样本。",
            entity_type="stock",
            category="sample_filter",
            group_name="stock_universe",
            output_type="boolean",
            frequency="daily",
            asset_id="stock",
            required_fields=required_fields,
            params={
                "_force_entity_asset_source": True,
                "data_processing": {
                    "winsorize": "none",
                    "standardize": "none",
                    "neutralize": [],
                },
                "weighting": "equal",
            },
            availability_policy={
                "field": "available_at",
                "policy": "entity_asset_point_in_time",
            },
            expression=combined_expression,
            enabled=True,
            version=1,
            available_versions=[1],
            definition_hash=identity,
        )
        start_date = pd.Timestamp(date_start).date()
        end_date = pd.Timestamp(date_end).date()
        with factor_query_source(
            factor,
            overrides={},
            date_start=start_date,
            date_end=end_date,
            job_id="model-sample-filter",
        ) as source_binding:
            plan = build_factor_query_plan(
                factor,
                overrides={},
                entity_type="stock",
                date_start=start_date,
                date_end=end_date,
                job_id="model-sample-filter",
                source_binding=source_binding,
            )
            rows = self.client.query(
                f"""
                SELECT trade_date, entity_code
                FROM (
                    {plan.sql}
                )
                WHERE score != 0
                  AND entity_code IN {{codes:Array(String)}}
                ORDER BY trade_date, entity_code
                """,
                parameters={
                    **plan.params,
                    "codes": sorted(
                        membership["instrument"].astype(str).unique().tolist()
                    ),
                },
            ).result_rows
        eligible = pd.DataFrame(rows, columns=["trade_date", "instrument"])
        if eligible.empty:
            return membership.iloc[0:0].copy()
        eligible["trade_date"] = pd.to_datetime(eligible["trade_date"])
        eligible["instrument"] = eligible["instrument"].astype(str)
        return membership.merge(
            eligible.drop_duplicates(["trade_date", "instrument"]),
            on=["trade_date", "instrument"],
            how="inner",
        )

    def _apply_legacy_custom_formula_filters(
        self,
        membership: pd.DataFrame,
        *,
        date_start: str,
        date_end: str,
        formulas: list[dict[str, Any]],
    ) -> pd.DataFrame:
        compiled = [
            compile_sample_filter_formula(str(item.get("expression") or ""))
            for item in formulas
        ]
        maximum_window = max(item.max_window for item in compiled)
        lookback_days = max(maximum_window * 4 + 20, 90)
        source_start = (
            pd.Timestamp(date_start) - timedelta(days=lookback_days)
        ).date().isoformat()
        database = _sql_identifier(
            str(self.settings.factor_database), "factor_database",
        )
        table = _sql_identifier(
            str(
                getattr(
                    self.settings,
                    "stock_daily_table",
                    "stock_daily_factor_source",
                )
            ),
            "stock_daily_table",
        )
        projections = [
            f"toUInt8(ifNull(({item.sql}) != 0, 0)) AS formula_{index}"
            for index, item in enumerate(compiled)
        ]
        predicates = [
            f"formula_{index} = 1" for index in range(len(compiled))
        ]
        rows = self.client.query(
            f"""
            SELECT trade_date, instrument
            FROM (
                SELECT
                    toDate(trade_time) AS trade_date,
                    code AS instrument,
                    {', '.join(projections)}
                FROM {database}.{table}
                WHERE code IN {{codes:Array(String)}}
                  AND toDate(trade_time) >= {{source_start:Date}}
                  AND toDate(trade_time) <= {{date_end:Date}}
            )
            WHERE trade_date >= {{date_start:Date}}
              AND {' AND '.join(predicates)}
            ORDER BY trade_date, instrument
            """,
            parameters={
                "codes": sorted(
                    membership["instrument"].astype(str).unique().tolist()
                ),
                "source_start": source_start,
                "date_start": date_start,
                "date_end": date_end,
            },
        ).result_rows
        eligible = pd.DataFrame(rows, columns=["trade_date", "instrument"])
        if eligible.empty:
            return membership.iloc[0:0].copy()
        eligible["trade_date"] = pd.to_datetime(eligible["trade_date"])
        eligible["instrument"] = eligible["instrument"].astype(str)
        return membership.merge(
            eligible.drop_duplicates(["trade_date", "instrument"]),
            on=["trade_date", "instrument"],
            how="inner",
        )

    def trading_dates_ending_at(
        self, trade_date: str, count: int, *,
        index_code: str = "000905.SH", universe_id: str = "csi500",
        data_bindings: dict[str, Any] | None = None,
    ) -> list[str]:
        """Return the last ``count`` benchmark sessions without crossing signal time."""
        requested = int(count)
        if requested < 1:
            raise ValueError("交易日窗口必须大于0")
        calendar_binding = frozen_data_binding(
            data_bindings, TRADING_CALENDAR_BINDING_ID,
        )
        if calendar_binding is not None:
            calendar_start = (
                pd.Timestamp(trade_date) - timedelta(days=requested * 3 + 30)
            ).date().isoformat()
            calendar = load_bound_trading_calendar(
                self.settings, calendar_binding, calendar_start, trade_date,
            )
            dates = [item.date().isoformat() for item in calendar[-requested:]]
            if (
                len(dates) < requested
                or dates[-1] != pd.Timestamp(trade_date).date().isoformat()
            ):
                raise ValueError(
                    f"{trade_date}之前没有足够的{requested}个交易日"
                )
            return dates
        calendar_code = "000905.SH" if universe_id == "all_a" else index_code
        rows = self.client.query(
            f"""
            SELECT trade_date
            FROM (
                SELECT DISTINCT toDate(trade_time) AS trade_date
                FROM {self.settings.source_database}.ad_market_kline_daily
                WHERE code = {{calendar_code:String}}
                  AND toDate(trade_time) <= {{trade_date:Date}}
                ORDER BY trade_date DESC
                LIMIT {{count:UInt32}}
            )
            ORDER BY trade_date
            """,
            parameters={
                "trade_date": trade_date, "count": requested,
                "calendar_code": calendar_code,
            },
        ).result_rows
        dates = [pd.Timestamp(row[0]).date().isoformat() for row in rows]
        if len(dates) < requested or dates[-1] != pd.Timestamp(trade_date).date().isoformat():
            raise ValueError(f"{trade_date}之前没有足够的{requested}个基准交易日")
        return dates

    def _factor_values(
        self,
        item: dict[str, Any],
        cutoff: datetime,
        date_start: str,
        date_end: str,
        *,
        cancellation: CancellationToken | None = None,
        resolved_factor: FactorOut | None = None,
    ) -> pd.DataFrame:
        signal_close = datetime.combine(
            pd.Timestamp(date_end).date(), datetime.min.time(),
        ).replace(hour=15)
        if cutoff < signal_close:
            raise ValueError(f"因子{item['factor_id']}的数据截止时间早于信号日收盘")
        entity_field = is_entity_field_feature(item)
        factor = resolved_factor or self._factor_definition(item)
        params = item.get("params")
        if not isinstance(params, dict):
            raise ValueError(f"冻结因子{item['factor_id']}缺少params")
        expected_hash = str(item.get("params_hash") or "").strip().lower()
        chunk_start = pd.Timestamp(date_start).date()
        final_end = pd.Timestamp(date_end).date()
        rows: list[tuple[Any, ...]] = []
        chunk_days = int(
            getattr(self, "factor_query_chunk_days", 366)
        )
        while chunk_start <= final_end:
            _checkpoint(cancellation)
            chunk_end = min(
                chunk_start + timedelta(days=chunk_days - 1),
                final_end,
            )
            with factor_query_source(
                factor,
                overrides=params,
                date_start=chunk_start,
                date_end=chunk_end,
                job_id="model-dataset",
            ) as source_binding:
                plan = build_factor_query_plan(
                    factor,
                    overrides=params,
                    entity_type="stock",
                    date_start=chunk_start,
                    date_end=chunk_end,
                    job_id="model-dataset",
                    source_binding=source_binding,
                )
                if not entity_field and plan.params_hash != expected_hash:
                    raise ValueError(
                        f"冻结因子{item['factor_id']}的params_hash与公式参数不一致"
                    )
                # The canonical query retains its own factor-specific lookback
                # for every chunk. Only requested output dates are concatenated,
                # so rolling windows stay identical at chunk boundaries.
                feature_sql = f"""
                SELECT trade_date, entity_code, score AS value
                FROM (
                    {plan.sql}
                )
                """
                rows.extend(
                    self.client.query(
                        feature_sql, parameters=plan.params,
                    ).result_rows
                )
            chunk_start = chunk_end + timedelta(days=1)
        frame = pd.DataFrame(rows, columns=["trade_date", "instrument", "value"])
        if not frame.empty:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
        return frame

    def _factor_values_batch(
        self,
        items: list[tuple[dict[str, Any], FactorOut]],
        cutoff: datetime,
        date_start: str,
        date_end: str,
        *,
        cancellation: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[tuple[str, str], pd.DataFrame]:
        """Stage one composite entity asset once for all same-source factors."""
        signal_close = datetime.combine(
            pd.Timestamp(date_end).date(), datetime.min.time(),
        ).replace(hour=15)
        if cutoff < signal_close:
            raise ValueError("批量因子的数据截止时间早于信号日收盘")
        fields = sorted({
            str(field)
            for _item, factor in items
            for field in factor.required_fields
            if str(field).strip()
        })
        if not fields:
            raise ValueError("批量实体资产因子缺少公式字段")
        lookback_days = 90
        for item, factor in items:
            params = item.get("params")
            if not isinstance(params, dict):
                raise ValueError(f"冻结因子{item['factor_id']}缺少params")
            compiled = compile_qlib_formula(
                factor.expression,
                params={**params, "window": params.get("window", 20)},
                code_column="code",
                date_column="trade_time",
            )
            lookback_days = max(
                lookback_days, compiled.max_window * 4 + 20,
            )
        rows_by_factor: dict[tuple[str, str], list[tuple[Any, ...]]] = {
            (
                str(item["factor_id"]),
                str(item.get("params_hash") or ""),
            ): []
            for item, _factor in items
        }
        chunk_start = pd.Timestamp(date_start).date()
        final_end = pd.Timestamp(date_end).date()
        # A wider chunk keeps rolling lookback overlap bounded while the
        # staged table remains small enough for one year of all-A daily data.
        chunk_days = max(366, int(self.factor_query_chunk_days))
        chunk_count = max(
            1,
            math.ceil(((final_end - chunk_start).days + 1) / chunk_days),
        )
        chunk_index = 0
        factor_settings = load_factor_settings()
        source_database = _sql_identifier(
            str(factor_settings.source_database), "source_database",
        )
        source_table = _sql_identifier(
            str(factor_settings.stock_daily_table), "stock_daily_table",
        )
        while chunk_start <= final_end:
            _checkpoint(cancellation)
            chunk_index += 1
            chunk_end = min(
                chunk_start + timedelta(days=chunk_days - 1), final_end,
            )
            source_start = chunk_start - timedelta(days=lookback_days)
            trading_rows = self.client.query(
                f"""
                SELECT DISTINCT toDate(trade_time) AS trade_date
                FROM {source_database}.{source_table}
                WHERE toDate(trade_time) >= {{date_start:Date}}
                  AND toDate(trade_time) <= {{date_end:Date}}
                ORDER BY trade_date
                """,
                parameters={
                    "date_start": source_start,
                    "date_end": chunk_end,
                },
            ).result_rows
            trading_dates = [
                pd.Timestamp(row[0]).date() for row in trading_rows
            ]
            _progress(progress, "loading_factor_source_batch", 8, {
                "factor_ids": [
                    str(item["factor_id"]) for item, _factor in items
                ],
                "field_count": len(fields),
                "chunk_index": chunk_index,
                "chunk_count": chunk_count,
            })
            with staged_entity_asset_source(
                db_client=self.client,
                database=self.settings.factor_database,
                api_base_url=self.settings.data_sdk_api_base_url,
                timeout_seconds=self.settings.data_sdk_query_timeout_seconds,
                concurrency=self.settings.data_sdk_query_concurrency,
                entity_id="stock",
                fields=fields,
                trading_dates=trading_dates,
                date_start=chunk_start,
                date_end=chunk_end,
                job_id="model-dataset-batch",
            ) as source_binding:
                for item, factor in items:
                    _checkpoint(cancellation)
                    params = dict(item["params"])
                    plan = build_factor_query_plan(
                        factor,
                        overrides=params,
                        entity_type="stock",
                        date_start=chunk_start,
                        date_end=chunk_end,
                        job_id="model-dataset",
                        source_binding=source_binding,
                    )
                    expected_hash = str(
                        item.get("params_hash") or ""
                    ).strip().lower()
                    if plan.params_hash != expected_hash:
                        raise ValueError(
                            f"冻结因子{item['factor_id']}的params_hash与公式参数不一致"
                        )
                    values = self.client.query(
                        f"""
                        SELECT trade_date, entity_code, score AS value
                        FROM ({plan.sql})
                        """,
                        parameters=plan.params,
                    ).result_rows
                    rows_by_factor[(
                        str(item["factor_id"]), expected_hash,
                    )].extend(values)
            chunk_start = chunk_end + timedelta(days=1)
        frames: dict[tuple[str, str], pd.DataFrame] = {}
        for key, rows in rows_by_factor.items():
            frame = pd.DataFrame(
                rows, columns=["trade_date", "instrument", "value"],
            )
            if not frame.empty:
                frame["trade_date"] = pd.to_datetime(frame["trade_date"])
                frame["value"] = pd.to_numeric(
                    frame["value"], errors="coerce",
                )
                frame = frame.dropna(subset=["value"])
            frames[key] = frame
        return frames

    @staticmethod
    def _factor_definition(item: dict[str, Any]) -> FactorOut:
        if is_entity_field_feature(item):
            return virtual_entity_field_factor(item)
        factor = factor_repository.get_factor(
            str(item["factor_id"]), version=int(item["factor_version"]),
        )
        if factor is None:
            raise ValueError(
                f"冻结因子不存在: {item['factor_id']} v{item['factor_version']}"
            )
        return factor

    def _adjusted_close(
        self,
        instruments: list[str],
        date_start: str,
        date_end: str,
        *,
        data_bindings: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        daily_binding = frozen_data_binding(
            data_bindings, STOCK_DAILY_BINDING_ID,
        )
        if daily_binding is not None:
            return load_bound_stock_daily(
                self.settings, daily_binding, instruments,
                date_start,
                (pd.Timestamp(date_end) + timedelta(days=10)).date().isoformat(),
            )[["trade_date", "instrument", "adjusted_close"]]
        rows = self.client.query(
            f"""
            SELECT
                toDate(k.trade_time) AS trade_date,
                k.code,
                k.close * ifNull(a.backward_adj_factor, 1.0) AS adjusted_close
            FROM {self.settings.source_database}.ad_market_kline_daily k
            ASOF LEFT JOIN (
                SELECT code AS adjustment_code, toDate(divid_operate_date) AS factor_date,
                       toFloat64OrNull(nullIf(back_adjust_factor, '')) AS backward_adj_factor
                FROM baostock.bs_adjust_factor
                WHERE code IN {{codes:Array(String)}}
                ORDER BY code, factor_date
            ) a ON k.code = a.adjustment_code AND toDate(k.trade_time) >= a.factor_date
            WHERE k.code IN {{codes:Array(String)}}
              AND toDate(k.trade_time) >= {{date_start:Date}}
              AND toDate(k.trade_time) <= {{date_end:Date}} + INTERVAL 10 DAY
              AND k.close IS NOT NULL AND k.close > 0
            ORDER BY trade_date, code
            """,
            parameters={"codes": instruments, "date_start": date_start, "date_end": date_end},
        ).result_rows
        frame = pd.DataFrame(rows, columns=["trade_date", "instrument", "adjusted_close"])
        if not frame.empty:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            frame["adjusted_close"] = pd.to_numeric(frame["adjusted_close"], errors="coerce")
        return frame

    def _industry_membership(
        self, observations: pd.DataFrame, date_start: str, date_end: str,
        *, industry_feature: dict[str, Any] | None = None,
        data_bindings: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        """Return exact-date Shenwan 2021 L1 membership for observed stocks.

        The source contains a retroactively restated history before the SW2021
        cutover.  Those older rows overlap legacy classifications, so accepting
        them would leak a future taxonomy into historical samples.  Starting at
        the cutover, the daily snapshot has one L1 industry per stock and can be
        joined using only the signal date.
        """
        empty = pd.DataFrame(columns=[
            "trade_date", "instrument", "industry_entity",
            "industry_name", "industry_weight",
        ])
        if observations.empty:
            return empty
        if pd.Timestamp(date_start) < pd.Timestamp(SW2021_INDUSTRY_SAFE_START):
            raise ValueError(
                "申万一级行业轮动仅支持2021-12-13及以后；"
                "更早历史包含申万2021版回溯重分类"
            )
        frozen_binding = (
            industry_feature.get("data_binding")
            if isinstance(industry_feature, dict)
            else None
        )
        if not isinstance(frozen_binding, dict):
            frozen_binding = frozen_data_binding(
                data_bindings, INDUSTRY_FEATURE_BINDING_ID,
            )
        if isinstance(frozen_binding, dict):
            return load_bound_industry_membership(
                self.settings, observations, frozen_binding,
            )
        expected = observations[["trade_date", "instrument"]].drop_duplicates().copy()
        expected["trade_date"] = pd.to_datetime(expected["trade_date"], errors="coerce")
        instruments = sorted(expected["instrument"].dropna().astype(str).unique())
        if not instruments:
            return empty
        rows = self.client.query(
            f"""
            SELECT w.trade_date, w.con_code, w.index_code, b.level1_name,
                   toFloat64(w.weight)
            FROM {self.settings.source_database}.ad_industry_weight w
            INNER JOIN {self.settings.source_database}.ad_industry_base_info b
              ON w.index_code = b.index_code
            WHERE b.level_type = 1
              AND w.con_code IN {{codes:Array(String)}}
              AND w.trade_date >= {{date_start:Date}}
              AND w.trade_date <= {{date_end:Date}}
              AND w.weight IS NOT NULL AND w.weight > 0
            ORDER BY w.trade_date, w.con_code, w.index_code
            """,
            parameters={
                "codes": instruments, "date_start": date_start,
                "date_end": date_end,
            },
        ).result_rows
        frame = pd.DataFrame(rows, columns=[
            "trade_date", "instrument", "industry_entity",
            "industry_name", "industry_weight",
        ])
        if frame.empty:
            raise ValueError("申万2021版一级行业日频权重为空")
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame["industry_weight"] = pd.to_numeric(
            frame["industry_weight"], errors="coerce",
        )
        frame = frame.dropna(subset=[
            "trade_date", "instrument", "industry_entity", "industry_weight",
        ])
        duplicate = frame.duplicated(
            ["trade_date", "instrument"], keep=False,
        )
        if duplicate.any():
            sample = frame.loc[duplicate, [
                "trade_date", "instrument", "industry_entity",
            ]].head(5).to_dict("records")
            raise ValueError(f"申万一级行业日频映射存在重复归属: {sample}")
        mapped = expected.merge(
            frame, on=["trade_date", "instrument"], how="inner",
        )
        coverage = len(mapped) / max(1, len(expected))
        if not np.isfinite(coverage) or coverage < 0.8:
            raise ValueError(f"申万一级行业日频映射覆盖率{coverage:.2%}低于80%")
        return mapped

    def industry_features(
        self, features: pd.DataFrame, feature_names: list[str],
        date_start: str, date_end: str,
        *, data_bindings: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        membership = self._industry_membership(
            features[["trade_date", "instrument"]], date_start, date_end,
            data_bindings=data_bindings,
        )
        result, _membership = _industry_features(
            features, membership, feature_names,
        )
        return result


def _expand_membership_intervals(
    calendar: pd.DatetimeIndex,
    intervals: pd.DataFrame,
) -> pd.DataFrame:
    """Expand PIT membership intervals onto the configured trading calendar."""
    empty = pd.DataFrame(columns=["trade_date", "instrument"])
    if calendar.empty or intervals.empty:
        return empty
    source = intervals.copy()
    source["in_date"] = pd.to_datetime(source["in_date"], errors="coerce")
    if "out_date" not in source:
        source["out_date"] = pd.NaT
    source["out_date"] = pd.to_datetime(source["out_date"], errors="coerce")
    source["instrument"] = source["instrument"].astype(str)
    source = source.dropna(subset=["instrument", "in_date"])
    parts: list[pd.DataFrame] = []
    for row in source[["instrument", "in_date", "out_date"]].itertuples(
        index=False,
    ):
        active = calendar[calendar >= row.in_date]
        if pd.notna(row.out_date):
            active = active[active <= row.out_date]
        if len(active):
            parts.append(pd.DataFrame({
                "trade_date": active,
                "instrument": str(row.instrument),
            }))
    if not parts:
        return empty
    return pd.concat(parts, ignore_index=True).drop_duplicates(
        ["trade_date", "instrument"], keep="last",
    ).sort_values(["trade_date", "instrument"], ignore_index=True)


def _entity_asset_batch_key(factor: FactorOut) -> str:
    raw_params = getattr(factor, "params", None)
    params = raw_params if isinstance(raw_params, dict) else {}
    source_asset = str(params.get("_source_asset") or "").strip()
    if not source_asset:
        return ""
    # The Data SDK stock daily view is a composite PIT projection. Factors
    # backed by different stock assets can therefore share one staged panel.
    entity_type = str(getattr(factor, "entity_type", "") or "").strip()
    return entity_type if entity_type else ""


def _future_rank_label(prices: pd.DataFrame, horizon: int) -> pd.DataFrame:
    if prices.empty:
        raise ValueError("后复权收盘价为空")
    pivot = prices.drop_duplicates(["trade_date", "instrument"], keep="last").pivot(
        index="trade_date", columns="instrument", values="adjusted_close",
    ).sort_index()
    future_return = pivot.shift(-int(horizon)).div(pivot).sub(1.0)
    percentile = future_return.rank(axis=1, pct=True, method="average")
    labels = (2.0 * percentile - 1.0).stack(future_stack=True).dropna().rename("LABEL0").reset_index()
    return labels


def _future_direction_label(prices: pd.DataFrame, horizon: int) -> pd.DataFrame:
    if prices.empty:
        raise ValueError("后复权收盘价为空")
    pivot = prices.drop_duplicates(["trade_date", "instrument"], keep="last").pivot(
        index="trade_date", columns="instrument", values="adjusted_close",
    ).sort_index()
    future_return = pivot.shift(-int(horizon)).div(pivot).sub(1.0)
    labels = future_return.gt(0.0).where(future_return.notna()).stack(
        future_stack=True,
    ).dropna().astype(float).rename("LABEL0").reset_index()
    return labels


def _industry_features(
    features: pd.DataFrame,
    membership: pd.DataFrame,
    feature_names: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if features.empty or membership.empty:
        raise ValueError("行业轮动训练缺少申万一级行业日频映射")
    source = features.merge(
        membership,
        on=["trade_date", "instrument"],
        how="inner",
        validate="one_to_one",
    )
    if source.empty:
        raise ValueError("所选股票与申万一级行业日频映射无交集")
    group_counts = source.groupby("trade_date")["industry_entity"].nunique()
    complete_dates = set(group_counts[group_counts >= 2].index)
    source = source[source["trade_date"].isin(complete_dates)].copy()
    if source.empty:
        raise ValueError("行业轮动无法形成每日行业截面")
    aggregated = _weighted_group_means(
        source,
        group_columns=["trade_date", "industry_entity"],
        value_columns=feature_names,
        weight_column="industry_weight",
    )
    aggregated.rename(columns={"industry_entity": "instrument"}, inplace=True)
    return aggregated, source[[
        "trade_date", "instrument", "industry_entity",
        "industry_name", "industry_weight",
    ]].copy()


def _industry_rank_label(
    prices: pd.DataFrame,
    membership: pd.DataFrame,
    *,
    horizon: int,
    classification: bool = False,
) -> pd.DataFrame:
    if prices.empty or membership.empty:
        raise ValueError("行业轮动标签缺少价格或行业映射")
    pivot = prices.drop_duplicates(
        ["trade_date", "instrument"], keep="last",
    ).pivot(
        index="trade_date", columns="instrument", values="adjusted_close",
    ).sort_index()
    returns = pivot.shift(-int(horizon)).div(pivot).sub(1.0)
    future = returns.stack(future_stack=True).dropna().rename(
        "future_return",
    ).reset_index()
    source = membership.merge(
        future, on=["trade_date", "instrument"], how="inner",
    )
    grouped = _weighted_group_means(
        source,
        group_columns=["trade_date", "industry_entity"],
        value_columns=["future_return"],
        weight_column="industry_weight",
    )
    grouped["_rank"] = grouped.groupby("trade_date")["future_return"].rank(
        method="average", ascending=True,
    )
    grouped["_count"] = grouped.groupby("trade_date")[
        "industry_entity"
    ].transform("size")
    grouped = grouped[grouped["_count"] >= 2].copy()
    grouped["LABEL0"] = (
        grouped["future_return"].gt(0.0).astype(float)
        if classification
        else 2.0 * (grouped["_rank"] - 1.0) / (grouped["_count"] - 1.0) - 1.0
    )
    grouped.rename(columns={"industry_entity": "instrument"}, inplace=True)
    return grouped[["trade_date", "instrument", "LABEL0"]]


def _weighted_group_means(
    frame: pd.DataFrame,
    *,
    group_columns: list[str],
    value_columns: list[str],
    weight_column: str,
) -> pd.DataFrame:
    """Compute per-column weighted means while ignoring only that column's NaNs."""
    result = frame[group_columns].drop_duplicates().copy()
    keys = [frame[column] for column in group_columns]
    weights = pd.to_numeric(frame[weight_column], errors="coerce")
    weights = weights.where(np.isfinite(weights) & (weights > 0))
    for column in value_columns:
        values = pd.to_numeric(frame[column], errors="coerce")
        valid = values.notna() & weights.notna() & np.isfinite(values)
        numerator = (values * weights).where(valid).groupby(keys).sum(min_count=1)
        denominator = weights.where(valid).groupby(keys).sum(min_count=1)
        grouped = numerator.div(denominator).rename(column).reset_index()
        result = result.merge(grouped, on=group_columns, how="left")
    return result.sort_values(group_columns).reset_index(drop=True)


def _normalized_calendar(dates: Any) -> pd.DatetimeIndex:
    calendar = pd.DatetimeIndex(
        pd.to_datetime(pd.Index(dates), errors="coerce")
    ).dropna()
    return pd.DatetimeIndex(
        calendar.normalize().unique()
    ).sort_values()


def _calendar_fingerprint(dates: pd.DatetimeIndex) -> str:
    payload = "\n".join(item.date().isoformat() for item in dates)
    return sha256(payload.encode("utf-8")).hexdigest()


def _split_from_calendar(
    dates: Any,
    split: dict[str, Any],
) -> dict[str, tuple[str, str]]:
    embargo_days = max(1, int(split.get("embargo_days") or 5))
    if str(split.get("mode") or "ratio") == "dates":
        return split_trading_dates_by_dates(
            pd.Index(dates),
            train=split.get("train"),
            valid=split.get("valid") or split.get("validation"),
            test=split.get("test"),
            embargo_days=embargo_days,
        )
    return split_trading_dates(
        pd.Index(dates),
        embargo_days=embargo_days,
        train_ratio=float(split.get("train") or 0.6),
        valid_ratio=float(split.get("valid") or 0.2),
    )


def resolve_dataset_split(
    calendar: Any,
    *,
    split: dict[str, Any],
    label_horizon_trading_days: int,
) -> dict[str, Any]:
    """Freeze calendar-derived split boundaries for deterministic replay."""

    normalized = _normalized_calendar(calendar)
    horizon = int(label_horizon_trading_days)
    if horizon < 1:
        raise ValueError("标签预测周期必须至少为1个交易日")
    if len(normalized) <= horizon:
        raise ValueError(f"训练日期范围不足以生成{horizon}交易日标签")
    trainable = normalized[:-horizon]
    segments = _split_from_calendar(trainable, split)
    return {
        "schema_version": DATASET_SPLIT_RESOLUTION_SCHEMA_VERSION,
        "segments": {
            name: list(value) for name, value in segments.items()
        },
        "calendar": {
            "fingerprint": _calendar_fingerprint(normalized),
            "session_count": len(normalized),
            "date_start": normalized[0].date().isoformat(),
            "date_end": normalized[-1].date().isoformat(),
            "trainable_fingerprint": _calendar_fingerprint(trainable),
            "trainable_session_count": len(trainable),
            "trainable_date_start": trainable[0].date().isoformat(),
            "trainable_date_end": trainable[-1].date().isoformat(),
            "label_horizon_trading_days": horizon,
            "embargo_days": max(1, int(split.get("embargo_days") or horizon)),
        },
    }


def materialized_dataset_segments(
    *,
    split: dict[str, Any],
    membership_calendar: Any,
    available_sample_dates: Any,
    label_horizon_trading_days: int,
) -> dict[str, tuple[str, str]]:
    """Reuse a preflight resolution; sample sparsity never moves boundaries."""

    frozen_resolution = split.get("resolved")
    if isinstance(frozen_resolution, dict):
        calendar = _normalized_calendar(membership_calendar)
        if calendar.empty:
            raise ValueError("冻结切分缺少训练时交易日历，无法校验漂移")
        current_resolution = resolve_dataset_split(
            calendar,
            split=split,
            label_horizon_trading_days=label_horizon_trading_days,
        )
        if current_resolution != frozen_resolution:
            raise ValueError("冻结交易日历或切分边界已漂移，拒绝重算Dataset")
        return {
            name: tuple(value)
            for name, value in current_resolution["segments"].items()
        }
    # Backward-compatible rebuild path for historical jobs created before
    # preflight froze the calendar-derived split resolution.
    return _split_from_calendar(available_sample_dates, split)


def split_trading_dates(
    dates: pd.Index,
    *,
    embargo_days: int = 5,
    train_ratio: float = 0.6,
    valid_ratio: float = 0.2,
) -> dict[str, tuple[str, str]]:
    unique = pd.Index(sorted(pd.to_datetime(dates).unique()))
    if len(unique) < 60:
        raise ValueError("有效交易日不足60天，无法切分训练/验证/测试集")
    train_ratio = float(train_ratio)
    valid_ratio = float(valid_ratio)
    test_ratio = round(1.0 - train_ratio - valid_ratio, 6)
    if not all(math.isfinite(ratio) for ratio in (train_ratio, valid_ratio, test_ratio)):
        raise ValueError("切分比例必须是有效数字")
    if train_ratio < 0.05 or valid_ratio < 0.05 or test_ratio < 0.05:
        raise ValueError("训练/验证/测试比例不得低于5%")
    if abs(train_ratio + valid_ratio + test_ratio - 1.0) > 1e-6:
        raise ValueError("训练/验证/测试比例之和必须为100%")
    train_boundary = int(len(unique) * train_ratio)
    valid_boundary = int(len(unique) * (train_ratio + valid_ratio))
    embargo = max(1, int(embargo_days))
    if valid_boundary <= train_boundary:
        raise ValueError("切分比例无效：验证集必须晚于训练集开始")
    train_end_index = train_boundary - embargo - 1
    valid_start_index = train_boundary
    valid_end_index = valid_boundary - embargo - 1
    test_start_index = valid_boundary
    if train_end_index < 0 or valid_end_index < valid_start_index:
        raise ValueError(f"数据范围不足以应用{embargo}交易日隔离")
    return {
        "train": (unique[0].date().isoformat(), unique[train_end_index].date().isoformat()),
        "valid": (unique[valid_start_index].date().isoformat(), unique[valid_end_index].date().isoformat()),
        "test": (unique[test_start_index].date().isoformat(), unique[-1].date().isoformat()),
    }


def split_trading_dates_by_dates(
    dates: pd.Index,
    *,
    train: Any,
    valid: Any,
    test: Any,
    embargo_days: int = 5,
) -> dict[str, tuple[str, str]]:
    """Validate an explicit split against the materialized trading calendar."""
    unique = pd.Index(sorted(pd.to_datetime(dates).normalize().unique()))
    if len(unique) < 3:
        raise ValueError("有效交易日不足，无法切分训练/验证/测试集")
    segments = {
        "train": _explicit_date_segment(train, "train"),
        "valid": _explicit_date_segment(valid, "validation"),
        "test": _explicit_date_segment(test, "test"),
    }
    calendar = set(unique)
    for name, (start, end) in segments.items():
        start_at = pd.Timestamp(start).normalize()
        end_at = pd.Timestamp(end).normalize()
        if start_at not in calendar or end_at not in calendar:
            raise ValueError(f"{name}切分边界必须是有效交易日")
        if start_at > end_at:
            raise ValueError(f"{name}切分开始日期不得晚于结束日期")
    train_start, train_end = map(pd.Timestamp, segments["train"])
    valid_start, valid_end = map(pd.Timestamp, segments["valid"])
    test_start, test_end = map(pd.Timestamp, segments["test"])
    if not (train_end < valid_start and valid_end < test_start):
        raise ValueError("训练/验证/测试日期必须严格有序且不得重叠")
    if train_start != unique[0] or test_end != unique[-1]:
        raise ValueError("显式切分必须覆盖完整可训练交易日范围")
    embargo = max(1, int(embargo_days))
    train_valid_gap = int(((unique > train_end) & (unique < valid_start)).sum())
    valid_test_gap = int(((unique > valid_end) & (unique < test_start)).sum())
    if train_valid_gap != embargo or valid_test_gap != embargo:
        raise ValueError(
            f"显式切分之间必须各保留恰好{embargo}个交易日隔离"
        )
    covered = (
        ((unique >= train_start) & (unique <= train_end))
        | ((unique >= valid_start) & (unique <= valid_end))
        | ((unique >= test_start) & (unique <= test_end))
        | ((unique > train_end) & (unique < valid_start))
        | ((unique > valid_end) & (unique < test_start))
    )
    if not bool(covered.all()):
        raise ValueError("显式切分存在未声明的交易日空档")
    return segments


def _explicit_date_segment(value: Any, name: str) -> tuple[str, str]:
    if isinstance(value, dict):
        start = value.get("start") or value.get("date_start")
        end = value.get("end") or value.get("date_end")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = value
    else:
        raise ValueError(f"{name}切分必须是[start, end]")
    try:
        start_at = pd.Timestamp(start).normalize()
        end_at = pd.Timestamp(end).normalize()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}切分日期无效") from exc
    return start_at.date().isoformat(), end_at.date().isoformat()


def walk_forward_segments(
    dates: pd.Index,
    *,
    strategy: str = "rolling",
    train_years: int = 3,
    valid_months: int = 6,
    test_months: int = 12,
    step_months: int = 12,
    max_windows: int = 4,
    embargo_days: int = 5,
) -> list[dict[str, tuple[str, str]]]:
    """Build recent, non-overlapping walk-forward windows from trading sessions.

    One year is defined as 252 observed trading sessions and one month as 21.
    All embargoes are counted using the supplied trading calendar. Windows are
    planned backwards from the latest observed session so the final independent
    test window always reaches the frozen dataset boundary. This avoids silently
    leaving a recent partial step outside the stability assessment.
    """
    unique = pd.Index(sorted(pd.to_datetime(dates).unique()))
    if strategy not in {"rolling", "expanding"}:
        raise ValueError("Walk-Forward策略只允许rolling或expanding")
    train_sessions = int(train_years) * 252
    valid_sessions = int(valid_months) * 21
    test_sessions = int(test_months) * 21
    step_sessions = int(step_months) * 21
    embargo = int(embargo_days)
    limit = int(max_windows)
    if train_sessions < 252 or valid_sessions < 21 or test_sessions < 21:
        raise ValueError("Walk-Forward训练、验证或测试窗口长度无效")
    if step_sessions < test_sessions:
        raise ValueError("Walk-Forward步长不得小于测试窗口，避免样本外预测日期重叠")
    if embargo < 1 or limit < 1:
        raise ValueError("Walk-Forward隔离天数和窗口数必须大于0")

    required = train_sessions + valid_sessions + test_sessions + embargo * 2
    if len(unique) < required:
        raise ValueError(
            f"有效交易日不足{required}天，无法生成Walk-Forward训练/验证/测试窗口"
        )

    windows: list[dict[str, tuple[str, str]]] = []
    for reverse_index in range(limit):
        test_end_index = len(unique) - 1 - reverse_index * step_sessions
        test_start_index = test_end_index - test_sessions + 1
        valid_end_index = test_start_index - embargo - 1
        valid_start_index = valid_end_index - valid_sessions + 1
        train_end_index = valid_start_index - embargo - 1
        train_start_index = (
            0 if strategy == "expanding"
            else train_end_index - train_sessions + 1
        )
        if train_start_index < 0:
            break
        windows.append({
            "train": _date_range(unique, train_start_index, train_end_index),
            "valid": _date_range(unique, valid_start_index, valid_end_index),
            "test": _date_range(unique, test_start_index, test_end_index),
        })
    return list(reversed(windows))


def _date_range(dates: pd.Index, start: int, end: int) -> tuple[str, str]:
    return (dates[start].date().isoformat(), dates[end].date().isoformat())


def _sql_identifier(value: str, label: str) -> str:
    if not SQL_IDENTIFIER_RE.fullmatch(str(value or "")):
        raise ValueError(f"{label}不是安全的ClickHouse标识符")
    return str(value)


def _feature_name(item: dict[str, Any]) -> str:
    return f"{item['factor_id']}__v{int(item['factor_version'])}__{str(item['params_hash'])[:8]}"


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    hashed = pd.util.hash_pandas_object(frame, index=True).values.tobytes()
    columns = json.dumps([list(item) for item in frame.columns], sort_keys=True).encode()
    return sha256(columns + hashed).hexdigest()


def _universe_filter_step(
    rule_id: str,
    before_count: int,
    after_count: int,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    before = max(0, int(before_count))
    after = max(0, int(after_count))
    return {
        "rule_id": str(rule_id),
        "before_count": before,
        "after_count": after,
        "removed_count": max(0, before - after),
        "removed_ratio": (
            round(max(0, before - after) / before, 8) if before else 0.0
        ),
        "params": dict(params or {}),
    }


def _checkpoint(cancellation: CancellationToken | None) -> None:
    if cancellation is not None:
        cancellation.checkpoint()


def _progress(
    callback: ProgressCallback | None, stage: str, percent: int, details: dict[str, Any],
) -> None:
    if callback is not None:
        callback(stage, percent, details)


__all__ = [
    "DatasetBuilder", "PreparedDataset", "split_trading_dates",
    "split_trading_dates_by_dates", "walk_forward_segments",
    "FACTOR_COMPUTED_CUTOFF", "FACTOR_EVENT_CUTOFF",
]
