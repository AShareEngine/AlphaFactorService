from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
import math
from typing import Any
from zoneinfo import ZoneInfo

import clickhouse_connect
import numpy as np
import pandas as pd

from factor_service import repository as factor_repository
from factor_service.entity_field_feature import (
    is_entity_field_feature,
    virtual_entity_field_factor,
)
from factor_service.factor_backtest import UNIVERSES
from factor_service.research.config import Settings
from factor_service.research.job import CancellationToken, ProgressCallback
from factor_service.worker import build_factor_query_plan, factor_query_source


FACTOR_COMPUTED_CUTOFF = "computed_at <= {cutoff:DateTime}"
FACTOR_EVENT_CUTOFF = (
    "event_available_at <= toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR"
)
SW2021_INDUSTRY_SAFE_START = "2021-12-13"
FACTOR_QUERY_CHUNK_DAYS = 366


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
        rows = self.client.query(
            """
            SELECT table, groupArray(name)
            FROM system.columns
            WHERE database = {database:String}
              AND table IN (
                  'ad_equity_structure', 'ad_industry_weight',
                  'ad_industry_base_info'
              )
            GROUP BY table
            """,
            parameters={"database": self.settings.source_database},
        ).result_rows
        columns = {str(table): {str(name) for name in names} for table, names in rows}
        equity_required = {
            "market_code", "ann_date", "change_date", "tot_share", "is_valid",
        }
        industry_weight_required = {
            "index_code", "con_code", "trade_date", "weight",
        }
        industry_base_required = {
            "index_code", "level_type", "level1_name",
        }
        equity_missing = sorted(
            equity_required - columns.get("ad_equity_structure", set())
        )
        industry_missing = sorted(
            {
                *(f"ad_industry_weight.{name}" for name in (
                    industry_weight_required
                    - columns.get("ad_industry_weight", set())
                )),
                *(f"ad_industry_base_info.{name}" for name in (
                    industry_base_required
                    - columns.get("ad_industry_base_info", set())
                )),
            }
        )
        return [
            {
                "target": "stock_selection",
                "label": "个股选股",
                "ready": True,
                "prediction_scope": "stock",
                "reason": "支持冻结T+1至T+30个股收益截面排名或涨跌方向标签。",
                "missing_fields": [],
            },
            {
                "target": "market_style",
                "label": "大小盘市场风格",
                "ready": not equity_missing,
                "prediction_scope": "market_style",
                "reason": (
                    "按公告日与变更日可用的总股本重建每日市值，形成大小盘两组。"
                    if not equity_missing else "缺少PIT市值重建字段。"
                ),
                "missing_fields": equity_missing,
            },
            {
                "target": "industry_rotation",
                "label": "申万一级行业轮动",
                "ready": not industry_missing,
                "prediction_scope": "industry",
                "reason": (
                    "使用申万2021版日频行业权重；仅允许从2021-12-13起训练，禁止使用更早的回溯重分类。"
                    if not industry_missing
                    else "缺少申万2021版日频行业权重或分类字段。"
                ),
                "missing_fields": industry_missing,
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
        coverage: dict[str, float] = {}
        _checkpoint(cancellation)
        _progress(progress, "building_membership", 6, {})
        universe_id = str(spec.get("universe_id") or "csi500")
        index_code = str(spec.get("index_code") or "000905.SH")
        membership = self._membership(
            date_start, date_end, universe_id=universe_id, index_code=index_code,
        )
        if membership.empty:
            raise ValueError("中证500历史成分股为空")
        expected = membership[["trade_date", "instrument"]].drop_duplicates()
        expected_count = max(1, len(expected))
        for index, item in enumerate(factors, start=1):
            _checkpoint(cancellation)
            name = str(item["factor_id"])
            _progress(progress, "loading_factors", 8 + int(26 * (index - 1) / len(factors)), {
                "factor_id": name, "factor_index": index, "factor_count": len(factors),
            })
            frame = self._factor_values(
                item, cutoff_for_clickhouse, date_start, date_end,
                cancellation=cancellation,
            )
            frame = frame.merge(expected, on=["trade_date", "instrument"], how="inner")
            actual_coverage = frame[["trade_date", "instrument"]].drop_duplicates().shape[0] / expected_count
            coverage[name] = actual_coverage
            if actual_coverage < float(spec.get("minimum_factor_coverage") or 0.8):
                raise ValueError(f"因子{name}覆盖率{actual_coverage:.2%}低于80%")
            feature_name = _feature_name(item)
            feature_names.append(feature_name)
            feature_frames.append(frame.rename(columns={"value": feature_name}))
        _checkpoint(cancellation)
        features = expected.copy()
        for frame in feature_frames:
            features = features.merge(frame, on=["trade_date", "instrument"], how="left")
        _progress(progress, "loading_prices", 38, {"instrument_count": int(features["instrument"].nunique())})
        prices = self._adjusted_close(sorted(features["instrument"].unique()), date_start, date_end)
        _checkpoint(cancellation)
        _progress(progress, "building_labels", 46, {})
        research_target = str(spec.get("research_target") or "stock_selection")
        label_spec = dict(spec.get("label") or {})
        horizon = int(label_spec.get("horizon_trading_days") or 5)
        target_mode = str(
            spec.get("target_mode") or label_spec.get("mode") or "return"
        ).strip().lower()
        classification = target_mode == "classification"
        target_contract: dict[str, Any]
        if research_target == "market_style":
            market_caps = self._historical_market_cap(
                sorted(features["instrument"].unique()), date_start, date_end,
            )
            features, style_membership = _market_style_features(
                features, market_caps, feature_names,
            )
            labels = _market_style_rank_label(
                prices, style_membership, horizon=horizon,
                classification=classification,
            )
            panel = features.merge(
                labels, on=["trade_date", "instrument"], how="inner",
            )
            target_contract = {
                "research_target": "market_style",
                "prediction_scope": "market_style",
                "entities": ["STYLE_SMALL", "STYLE_LARGE"],
                "feature_aggregation": "daily_group_mean",
                "membership": "daily_pit_market_cap_halves",
                "target_mode": target_mode,
                "label": (
                    "future_group_return_direction"
                    if classification
                    else "future_group_return_cross_sectional_rank"
                ),
            }
        elif research_target == "industry_rotation":
            industry_membership = self._industry_membership(
                expected, date_start, date_end,
            )
            features, industry_membership = _industry_features(
                features, industry_membership, feature_names,
            )
            labels = _industry_rank_label(
                prices, industry_membership, horizon=horizon,
                classification=classification,
            )
            panel = features.merge(
                labels, on=["trade_date", "instrument"], how="inner",
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
            panel = features.merge(
                labels, on=["trade_date", "instrument"], how="inner",
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
        panel.sort_values(["trade_date", "instrument"], inplace=True)
        trading_dates = pd.Index(sorted(panel["trade_date"].unique()))
        split_config = dict(spec.get("split") or {})
        segments = split_trading_dates(
            trading_dates,
            embargo_days=max(1, int(split_config.get("embargo_days") or 5)),
            train_ratio=float(split_config.get("train") or 0.6),
            valid_ratio=float(split_config.get("valid") or 0.2),
        )
        _progress(progress, "splitting_dataset", 52, {"segments": segments})
        train_start, train_end = segments["train"]
        train_mask = panel["trade_date"].between(pd.Timestamp(train_start), pd.Timestamp(train_end))
        if classification:
            train_classes = set(
                pd.to_numeric(panel.loc[train_mask, "LABEL0"], errors="coerce")
                .dropna()
                .astype(int)
                .unique()
                .tolist()
            )
            if train_classes != {0, 1}:
                raise ValueError("分类目标训练段必须同时包含上涨和下跌样本")
        medians = {
            name: float(pd.to_numeric(panel.loc[train_mask, name], errors="coerce").median())
            for name in feature_names
        }
        if any(not np.isfinite(value) for value in medians.values()):
            missing = [name for name, value in medians.items() if not np.isfinite(value)]
            raise ValueError("训练段无法计算因子中位数: " + ", ".join(missing))
        _checkpoint(cancellation)
        raw_indexed = panel.set_index(["trade_date", "instrument"])
        raw_indexed.index.names = ["datetime", "instrument"]
        raw_indexed = raw_indexed[feature_names + ["LABEL0"]]
        raw_indexed.columns = pd.MultiIndex.from_tuples(
            [("feature", name) for name in feature_names] + [("label", "LABEL0")]
        )
        indexed = raw_indexed.copy()
        indexed.loc[:, pd.IndexSlice["feature", :]] = (
            indexed.loc[:, pd.IndexSlice["feature", :]].fillna(medians)
        )
        future_function_guards = [
            "factor definitions and parameters frozen before materialization",
            "source rows limited to signal date and available by market close",
            "data_cutoff >= final signal date close",
            "historical index membership",
            "five session split embargo",
            "preprocessors fitted on train only",
        ]
        if research_target == "market_style":
            future_function_guards.append(
                "market-style membership uses close-date market cap and shares available by signal date"
            )
        elif research_target == "industry_rotation":
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
            "segments": segments,
            "data_cutoff": cutoff.isoformat(),
            "target_contract": target_contract,
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

    def _membership(
        self,
        date_start: str,
        date_end: str,
        *,
        universe_id: str = "csi500",
        index_code: str = "000905.SH",
    ) -> pd.DataFrame:
        if universe_id not in UNIVERSES:
            raise ValueError(f"不支持的股票池: {universe_id}")
        if index_code not in {config["index_code"] for config in UNIVERSES.values()}:
            raise ValueError(f"不支持的基准指数: {index_code}")
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
        return frame

    def trading_dates_ending_at(
        self, trade_date: str, count: int, *,
        index_code: str = "000905.SH", universe_id: str = "csi500",
    ) -> list[str]:
        """Return the last ``count`` benchmark sessions without crossing signal time."""
        requested = int(count)
        if requested < 1:
            raise ValueError("交易日窗口必须大于0")
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
    ) -> pd.DataFrame:
        signal_close = datetime.combine(
            pd.Timestamp(date_end).date(), datetime.min.time(),
        ).replace(hour=15)
        if cutoff < signal_close:
            raise ValueError(f"因子{item['factor_id']}的数据截止时间早于信号日收盘")
        entity_field = is_entity_field_feature(item)
        if entity_field:
            factor = virtual_entity_field_factor(item)
        else:
            factor = factor_repository.get_factor(
                str(item["factor_id"]), version=int(item["factor_version"]),
            )
            if factor is None:
                raise ValueError(
                    f"冻结因子不存在: {item['factor_id']} v{item['factor_version']}"
                )
        params = item.get("params")
        if not isinstance(params, dict):
            raise ValueError(f"冻结因子{item['factor_id']}缺少params")
        expected_hash = str(item.get("params_hash") or "").strip().lower()
        chunk_start = pd.Timestamp(date_start).date()
        final_end = pd.Timestamp(date_end).date()
        rows: list[tuple[Any, ...]] = []
        while chunk_start <= final_end:
            _checkpoint(cancellation)
            chunk_end = min(
                chunk_start + timedelta(days=FACTOR_QUERY_CHUNK_DAYS - 1),
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

    def _adjusted_close(self, instruments: list[str], date_start: str, date_end: str) -> pd.DataFrame:
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

    def _historical_market_cap(
        self, instruments: list[str], date_start: str, date_end: str,
    ) -> pd.DataFrame:
        price_rows = self.client.query(
            f"""
            SELECT toDate(trade_time), code, toFloat64(close)
            FROM {self.settings.source_database}.ad_market_kline_daily
            WHERE code IN {{codes:Array(String)}}
              AND toDate(trade_time) >= {{date_start:Date}}
              AND toDate(trade_time) <= {{date_end:Date}}
              AND close IS NOT NULL AND close > 0
            ORDER BY code, trade_time
            """,
            parameters={
                "codes": instruments, "date_start": date_start,
                "date_end": date_end,
            },
        ).result_rows
        share_rows = self.client.query(
            f"""
            SELECT market_code, ann_date, change_date, toFloat64(tot_share)
            FROM {self.settings.source_database}.ad_equity_structure
            WHERE market_code IN {{codes:Array(String)}}
              AND is_valid = 1 AND tot_share IS NOT NULL AND tot_share > 0
              AND ann_date <= {{date_end:Date}}
              AND change_date <= {{date_end:Date}}
            ORDER BY market_code, ann_date, change_date
            """,
            parameters={"codes": instruments, "date_end": date_end},
        ).result_rows
        prices = pd.DataFrame(
            price_rows, columns=["trade_date", "instrument", "close"],
        )
        shares = pd.DataFrame(
            share_rows,
            columns=["instrument", "ann_date", "change_date", "total_share_10k"],
        )
        if prices.empty or shares.empty:
            return pd.DataFrame(columns=["trade_date", "instrument", "market_cap"])
        prices["trade_date"] = pd.to_datetime(prices["trade_date"], errors="coerce")
        prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
        shares["ann_date"] = pd.to_datetime(shares["ann_date"], errors="coerce")
        shares["change_date"] = pd.to_datetime(shares["change_date"], errors="coerce")
        shares["total_share_10k"] = pd.to_numeric(
            shares["total_share_10k"], errors="coerce",
        )
        shares["available_date"] = shares[["ann_date", "change_date"]].max(axis=1)
        shares = shares.dropna(subset=["available_date", "total_share_10k"])
        parts: list[pd.DataFrame] = []
        share_groups = {
            str(code): group.sort_values("available_date")
            for code, group in shares.groupby("instrument", sort=False)
        }
        for code, price_group in prices.groupby("instrument", sort=False):
            available = share_groups.get(str(code))
            if available is None or available.empty:
                continue
            parts.append(pd.merge_asof(
                price_group.sort_values("trade_date"),
                available[["available_date", "total_share_10k"]],
                left_on="trade_date", right_on="available_date", direction="backward",
            ))
        if not parts:
            return pd.DataFrame(columns=["trade_date", "instrument", "market_cap"])
        result = pd.concat(parts, ignore_index=True).dropna(subset=["total_share_10k"])
        result["market_cap"] = result["close"] * result["total_share_10k"] * 10_000.0
        result = result.loc[
            np.isfinite(result["market_cap"]) & (result["market_cap"] > 0)
        ]
        return result[["trade_date", "instrument", "market_cap"]]

    def _industry_membership(
        self, observations: pd.DataFrame, date_start: str, date_end: str,
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

    def market_style_features(
        self, features: pd.DataFrame, feature_names: list[str],
        date_start: str, date_end: str,
    ) -> pd.DataFrame:
        market_caps = self._historical_market_cap(
            sorted(features["instrument"].astype(str).unique()),
            date_start, date_end,
        )
        result, _membership = _market_style_features(
            features, market_caps, feature_names,
        )
        return result

    def industry_features(
        self, features: pd.DataFrame, feature_names: list[str],
        date_start: str, date_end: str,
    ) -> pd.DataFrame:
        membership = self._industry_membership(
            features[["trade_date", "instrument"]], date_start, date_end,
        )
        result, _membership = _industry_features(
            features, membership, feature_names,
        )
        return result


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
    ).astype(float).rename("LABEL0").reset_index()
    return labels


def _market_style_features(
    features: pd.DataFrame,
    market_caps: pd.DataFrame,
    feature_names: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if features.empty or market_caps.empty:
        raise ValueError("大小盘风格训练缺少PIT市值数据")
    source = features.merge(
        market_caps, on=["trade_date", "instrument"], how="left",
    )
    coverage = source["market_cap"].notna().mean()
    if not np.isfinite(coverage) or coverage < 0.8:
        raise ValueError(f"大小盘风格PIT市值覆盖率{coverage:.2%}低于80%")
    source = source.dropna(subset=["market_cap"]).sort_values(
        ["trade_date", "market_cap", "instrument"],
    )
    source["_position"] = source.groupby("trade_date").cumcount()
    source["_count"] = source.groupby("trade_date")["instrument"].transform("size")
    source["style_entity"] = np.where(
        source["_position"] < (source["_count"] / 2.0),
        "STYLE_SMALL", "STYLE_LARGE",
    )
    group_counts = source.groupby("trade_date")["style_entity"].nunique()
    complete_dates = set(group_counts[group_counts == 2].index)
    source = source[source["trade_date"].isin(complete_dates)]
    if source.empty:
        raise ValueError("大小盘风格无法形成每日两个市值组")
    membership = source[[
        "trade_date", "instrument", "style_entity", "market_cap",
    ]].copy()
    aggregated = source.groupby(
        ["trade_date", "style_entity"], as_index=False,
    )[feature_names].mean()
    aggregated.rename(columns={"style_entity": "instrument"}, inplace=True)
    return aggregated, membership


def _market_style_rank_label(
    prices: pd.DataFrame,
    membership: pd.DataFrame,
    *,
    horizon: int,
    classification: bool = False,
) -> pd.DataFrame:
    if prices.empty or membership.empty:
        raise ValueError("大小盘风格标签缺少价格或市值分组")
    pivot = prices.drop_duplicates(
        ["trade_date", "instrument"], keep="last",
    ).pivot(
        index="trade_date", columns="instrument", values="adjusted_close",
    ).sort_index()
    returns = pivot.shift(-int(horizon)).div(pivot).sub(1.0)
    future = returns.stack(future_stack=True).dropna().rename(
        "future_return",
    ).reset_index()
    grouped = membership.merge(
        future, on=["trade_date", "instrument"], how="inner",
    ).groupby(
        ["trade_date", "style_entity"], as_index=False,
    )["future_return"].mean()
    grouped["_rank"] = grouped.groupby("trade_date")["future_return"].rank(
        method="average", ascending=True,
    )
    grouped["_count"] = grouped.groupby("trade_date")["style_entity"].transform(
        "size",
    )
    grouped = grouped[grouped["_count"] == 2].copy()
    grouped["LABEL0"] = (
        grouped["future_return"].gt(0.0).astype(float)
        if classification
        else 2.0 * (grouped["_rank"] - 1.0) / (grouped["_count"] - 1.0) - 1.0
    )
    grouped.rename(columns={"style_entity": "instrument"}, inplace=True)
    return grouped[["trade_date", "instrument", "LABEL0"]]


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


def _feature_name(item: dict[str, Any]) -> str:
    return f"{item['factor_id']}__v{int(item['factor_version'])}__{str(item['params_hash'])[:8]}"


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    hashed = pd.util.hash_pandas_object(frame, index=True).values.tobytes()
    columns = json.dumps([list(item) for item in frame.columns], sort_keys=True).encode()
    return sha256(columns + hashed).hexdigest()


def _checkpoint(cancellation: CancellationToken | None) -> None:
    if cancellation is not None:
        cancellation.checkpoint()


def _progress(
    callback: ProgressCallback | None, stage: str, percent: int, details: dict[str, Any],
) -> None:
    if callback is not None:
        callback(stage, percent, details)


__all__ = [
    "DatasetBuilder", "PreparedDataset", "split_trading_dates", "walk_forward_segments",
    "FACTOR_COMPUTED_CUTOFF", "FACTOR_EVENT_CUTOFF",
]
