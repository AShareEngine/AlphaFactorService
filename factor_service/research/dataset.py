from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any
from zoneinfo import ZoneInfo

import clickhouse_connect
import numpy as np
import pandas as pd

from factor_service import repository as factor_repository
from factor_service.research.config import Settings
from factor_service.research.job import CancellationToken, ProgressCallback
from factor_service.worker import build_factor_query_plan


FACTOR_COMPUTED_CUTOFF = "computed_at <= {cutoff:DateTime}"
FACTOR_EVENT_CUTOFF = (
    "event_available_at <= toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR"
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
        }

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
        membership = self._membership(date_start, date_end)
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
            frame = self._factor_values(item, cutoff_for_clickhouse, date_start, date_end)
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
        labels = _future_rank_label(prices, horizon=5)
        panel = features.merge(labels, on=["trade_date", "instrument"], how="inner")
        panel.sort_values(["trade_date", "instrument"], inplace=True)
        trading_dates = pd.Index(sorted(panel["trade_date"].unique()))
        segments = split_trading_dates(trading_dates, embargo_days=5)
        _progress(progress, "splitting_dataset", 52, {"segments": segments})
        train_start, train_end = segments["train"]
        train_mask = panel["trade_date"].between(pd.Timestamp(train_start), pd.Timestamp(train_end))
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
            "future_function_guards": [
                "factor definitions and parameters frozen before materialization",
                "source rows limited to signal date and available by market close",
                "data_cutoff >= final signal date close",
                "historical index membership",
                "five session split embargo",
                "preprocessors fitted on train only",
            ],
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

    def _membership(self, date_start: str, date_end: str) -> pd.DataFrame:
        rows = self.client.query(
            f"""
            SELECT calendar.trade_date, members.con_code
            FROM (
                SELECT DISTINCT toDate(trade_time) AS trade_date
                FROM {self.settings.source_database}.ad_market_kline_daily
                WHERE code = '000905.SH'
                  AND toDate(trade_time) >= {{date_start:Date}}
                  AND toDate(trade_time) <= {{date_end:Date}}
            ) AS calendar
            CROSS JOIN (
                SELECT con_code, in_date, out_date
                FROM {self.settings.source_database}.ad_index_constituent
                WHERE index_code = '000905.SH'
                  AND in_date <= {{date_end:Date}}
                  AND (out_date IS NULL OR out_date >= {{date_start:Date}})
            ) AS members
            WHERE members.in_date <= calendar.trade_date
              AND (members.out_date IS NULL OR members.out_date >= calendar.trade_date)
            ORDER BY calendar.trade_date, members.con_code
            """,
            parameters={"date_start": date_start, "date_end": date_end},
        ).result_rows
        frame = pd.DataFrame(rows, columns=["trade_date", "instrument"])
        if not frame.empty:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        return frame

    def trading_dates_ending_at(self, trade_date: str, count: int) -> list[str]:
        """Return the last ``count`` benchmark sessions without crossing signal time."""
        requested = int(count)
        if requested < 1:
            raise ValueError("交易日窗口必须大于0")
        rows = self.client.query(
            f"""
            SELECT trade_date
            FROM (
                SELECT DISTINCT toDate(trade_time) AS trade_date
                FROM {self.settings.source_database}.ad_market_kline_daily
                WHERE code = '000905.SH'
                  AND toDate(trade_time) <= {{trade_date:Date}}
                ORDER BY trade_date DESC
                LIMIT {{count:UInt32}}
            )
            ORDER BY trade_date
            """,
            parameters={"trade_date": trade_date, "count": requested},
        ).result_rows
        dates = [pd.Timestamp(row[0]).date().isoformat() for row in rows]
        if len(dates) < requested or dates[-1] != pd.Timestamp(trade_date).date().isoformat():
            raise ValueError(f"{trade_date}之前没有足够的{requested}个基准交易日")
        return dates

    def _factor_values(
        self, item: dict[str, Any], cutoff: datetime, date_start: str, date_end: str,
    ) -> pd.DataFrame:
        signal_close = datetime.combine(
            pd.Timestamp(date_end).date(), datetime.min.time(),
        ).replace(hour=15)
        if cutoff < signal_close:
            raise ValueError(f"因子{item['factor_id']}的数据截止时间早于信号日收盘")
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
        plan = build_factor_query_plan(
            factor,
            overrides=params,
            entity_type="stock",
            date_start=pd.Timestamp(date_start).date(),
            date_end=pd.Timestamp(date_end).date(),
            job_id="model-dataset",
        )
        expected_hash = str(item.get("params_hash") or "").strip().lower()
        if plan.params_hash != expected_hash:
            raise ValueError(
                f"冻结因子{item['factor_id']}的params_hash与公式参数不一致"
            )
        rows = self.client.query(plan.sql, parameters=plan.params).result_rows
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


def split_trading_dates(dates: pd.Index, *, embargo_days: int = 5) -> dict[str, tuple[str, str]]:
    unique = pd.Index(sorted(pd.to_datetime(dates).unique()))
    if len(unique) < 60:
        raise ValueError("有效交易日不足60天，无法切分训练/验证/测试集")
    train_boundary = int(len(unique) * 0.6)
    valid_boundary = int(len(unique) * 0.8)
    embargo = max(1, int(embargo_days))
    train_end_index = train_boundary - embargo - 1
    valid_start_index = train_boundary
    valid_end_index = valid_boundary - embargo - 1
    test_start_index = valid_boundary
    if train_end_index < 0 or valid_end_index < valid_start_index:
        raise ValueError("数据范围不足以应用5交易日隔离")
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
    All embargoes are counted using the supplied trading calendar.  When more
    windows are available than requested, the most recent windows are retained.
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
    offset = 0
    while True:
        train_start_index = 0 if strategy == "expanding" else offset
        train_end_index = offset + train_sessions - 1
        valid_start_index = train_end_index + embargo + 1
        valid_end_index = valid_start_index + valid_sessions - 1
        test_start_index = valid_end_index + embargo + 1
        test_end_index = test_start_index + test_sessions - 1
        if test_end_index >= len(unique):
            break
        windows.append({
            "train": _date_range(unique, train_start_index, train_end_index),
            "valid": _date_range(unique, valid_start_index, valid_end_index),
            "test": _date_range(unique, test_start_index, test_end_index),
        })
        offset += step_sessions
    return windows[-limit:]


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
