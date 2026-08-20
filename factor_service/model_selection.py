"""
模型推理信号的选股 / 负分参考 / 事件驱动回测（对齐 QuantMind 推理中心）。

数据流:
  model_predictions_daily（PIT 安全） → 申万行业映射 → 行业信号 / 市场状态
      → 个股分数区间 + 主板 + ST/涨跌停 + 3 天趋势过滤 → 候选股 / 做空参考
      → 事件驱动模拟: T+1 开盘买入 → 持有到期 / 止盈止损 / 行业转弱 / 大盘 MA20 清仓
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from factor_service.clickhouse import client, settings
from factor_service.model_repository import (
    _historical_industry_mapping,
    _historical_market_cap_frame,
)

logger = logging.getLogger(__name__)

# 行业信号阈值（与 QuantMind 一致）
_INDUSTRY_STRONG_SCORE = 0.10


# ---------------------------------------------------------------------------
# 策略参数
# ---------------------------------------------------------------------------

@dataclass
class StrategyConfig:
    """选股策略参数。默认值 = 平衡型（与 QuantMind 默认组合一致）。"""

    # 入场/空仓（行业 avg Top1）
    entry_threshold: float = 0.09      # 行业avgTop1 ≥ 此值才入场
    exit_threshold: float = 0.06       # 行业avgTop1 < 此值强制空仓
    strong_industry_min: int = 2       # 强行业数（Top1≥0.10）≥ 此值才入场

    # 个股分数区间
    score_min: float = 0.10
    score_max: float = 0.12

    # 交易
    initial_capital: float = 100_000.0
    max_hold_days: int = 5             # 最长持有交易日
    take_profit: float = 0.08          # 止盈 +8%
    stop_loss: float = 0.05            # 止损 -5%
    max_positions: int = 5             # 每日最多持有股票数
    daily_select_max: int = 5          # 每日新选股上限

    # 过滤开关
    exclude_limit_moves: bool = True   # 涨停买不进/跌停卖不出
    exclude_st: bool = True            # 剔除 ST
    main_board_only: bool = True       # 仅主板（600/000 开头）
    use_index_ma20_filter: bool = True # 大盘跌破 MA20 强制空仓
    index_symbol: str = "000001.SH"    # 上证指数

    # 数据源
    signal_mode: str = "stored"        # stored=读已有模型预测

    @classmethod
    def preset(cls, name: str) -> "StrategyConfig":
        """策略风格预设。"""
        base = cls()
        if name == "conservative":
            base.entry_threshold = 0.10
            base.exit_threshold = 0.10
            base.strong_industry_min = 5
        elif name == "aggressive":
            base.entry_threshold = 0.07
            base.exit_threshold = 0.06
            base.strong_industry_min = 1
        return base

    @classmethod
    def from_mapping(cls, values: dict[str, Any] | None) -> "StrategyConfig":
        cfg = cls()
        if not values:
            return cfg
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        for key, value in values.items():
            if key not in allowed or value is None:
                continue
            field_def = cls.__dataclass_fields__[key]
            try:
                if isinstance(value, bool) and field_def.type in ("bool", "bool | None"):
                    setattr(cfg, key, bool(value))
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    if field_def.type in ("int", "int | None"):
                        setattr(cfg, key, int(value))
                    else:
                        setattr(cfg, key, float(value))
                elif isinstance(value, str):
                    setattr(cfg, key, str(value))
                else:
                    setattr(cfg, key, value)
            except (TypeError, ValueError):
                continue
        return cfg


@dataclass
class Position:
    symbol: str
    name: str
    industry: str
    score: float
    buy_date: str
    buy_price: float
    shares: int
    hold_days: int = 0
    sell_date: str | None = None
    sell_price: float | None = None
    sell_reason: str | None = None
    profit_pct: float = 0.0
    open_pnl: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class Trade:
    date: str
    symbol: str
    name: str
    side: str            # BUY / SELL
    price: float
    shares: int
    amount: float
    industry: str
    score: float
    reason: str = ""
    profit_pct: float = 0.0
    hold_days: int = 0


@dataclass
class DailySelection:
    trade_date: str
    market_state: str
    industry_avg_top1: float
    strong_industry_count: int
    index_above_ma20: bool
    selections: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BacktestResult:
    status: str
    metrics: dict[str, Any]
    daily_selections: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    nav_curve: list[dict[str, Any]]
    holdings_snapshot: list[dict[str, Any]]
    industry_rotation: list[dict[str, Any]]
    monthly_returns: dict[str, float]
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 数据加载（ClickHouse，PIT 安全）
# ---------------------------------------------------------------------------

def _load_prediction_panel(
    *, model_id: str, model_version: int,
    trade_date: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
) -> pd.DataFrame:
    """读取模型预测（score 截面排名，PIT 安全：feature_cutoff_at ≤ 信号日15:00）。"""
    database = settings().model_database
    date_filter = ""
    params: dict[str, Any] = {
        "model_id": model_id,
        "model_version": int(model_version),
    }
    if trade_date:
        date_filter = "AND trade_date = {trade_date:Date}"
        params["trade_date"] = date.fromisoformat(str(trade_date)[:10])
    elif date_start and date_end:
        date_filter = """
          AND trade_date >= {date_start:Date}
          AND trade_date <= {date_end:Date}
        """
        params["date_start"] = date.fromisoformat(str(date_start)[:10])
        params["date_end"] = date.fromisoformat(str(date_end)[:10])
    rows = client().query(
        f"""
        WITH latest AS (
            SELECT trade_date, entity_code,
                   argMax(score, tuple(updated_at, computed_at, inference_run_id)) AS prediction,
                   argMax(feature_cutoff_at,
                          tuple(updated_at, computed_at, inference_run_id)) AS feature_cutoff_at
            FROM {database}.model_predictions_daily FINAL
            WHERE model_id = {{model_id:String}}
              AND model_version = {{model_version:UInt32}}
              {date_filter}
            GROUP BY trade_date, entity_code
        )
        SELECT trade_date, entity_code, prediction
        FROM latest
        WHERE feature_cutoff_at <=
              toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR
        ORDER BY trade_date, entity_code
        """,
        parameters=params,
    ).result_rows
    frame = pd.DataFrame(
        rows, columns=["trade_date", "instrument", "score"],
    )
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["score"] = pd.to_numeric(frame["score"], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "score"])
    return frame


def _latest_prediction_date(*, model_id: str, model_version: int) -> str | None:
    """查询模型最近一个预测交易日（轻量）。"""
    database = settings().model_database
    rows = client().query(
        f"""
        SELECT max(trade_date)
        FROM {database}.model_predictions_daily FINAL
        WHERE model_id = {{model_id:String}}
          AND model_version = {{model_version:UInt32}}
        """,
        parameters={"model_id": model_id, "model_version": int(model_version)},
    ).result_rows
    return str(rows[0][0]) if rows and rows[0][0] else None


def _load_price_panel(
    *, codes: list[str], date_start: str, date_end: str,
) -> pd.DataFrame:
    """加载回测区间的每日价格面板（前复权 open/high/low/close）。

    baostock.bs_adjust_factor.back_adjust_factor 用于前复权；
    ad_history_stock_status 提供 ST / 停牌 / 涨跌停状态。
    """
    if not codes:
        return pd.DataFrame(columns=[
            "date", "code", "open", "high", "low", "close",
            "pct_change", "is_st", "is_suspended", "high_limit", "low_limit",
        ])
    rows = client().query(
        """
        SELECT
            toDate(k.trade_time) AS trade_date,
            k.code,
            toFloat64(k.open) AS open,
            toFloat64(k.high) AS high,
            toFloat64(k.low) AS low,
            toFloat64(k.close) AS close,
            toFloat64(k.close) / if(toFloat64(ifNull(s.preclose, 0)) > 0,
                                          toFloat64(s.preclose), toFloat64(k.open))
                - 1.0 AS pct_change,
            toUInt8(ifNull(s.is_st_sec, '') IN ('1','true','True')) AS is_st,
            toUInt8(ifNull(s.is_susp_sec, '') IN ('1','true','True')) AS is_suspended,
            s.high_limited AS high_limit,
            s.low_limited AS low_limit,
            a.backward_adj_factor AS adj_factor
        FROM starlight.ad_market_kline_daily k
        ASOF LEFT JOIN (
            SELECT code AS adjustment_code, toDate(divid_operate_date) AS factor_date,
                   toFloat64OrNull(nullIf(back_adjust_factor, '')) AS backward_adj_factor
            FROM baostock.bs_adjust_factor
            WHERE code IN {codes:Array(String)}
            ORDER BY code, factor_date
        ) a ON k.code = a.adjustment_code AND toDate(k.trade_time) >= a.factor_date
        ANY LEFT JOIN starlight.ad_history_stock_status s
          ON k.code = s.market_code AND toDate(k.trade_time) = s.trade_date
        WHERE k.code IN {codes:Array(String)}
          AND toDate(k.trade_time) >= {date_start:Date} - INTERVAL 160 DAY
          AND toDate(k.trade_time) <= {date_end:Date} + INTERVAL 10 DAY
          AND k.open IS NOT NULL AND k.high IS NOT NULL
          AND k.low IS NOT NULL AND k.close IS NOT NULL
        ORDER BY trade_date, code
        """,
        parameters={
            "codes": codes,
            "date_start": date.fromisoformat(str(date_start)[:10]),
            "date_end": date.fromisoformat(str(date_end)[:10]),
        },
    ).result_rows
    frame = pd.DataFrame(rows, columns=[
        "date", "code", "open", "high", "low", "close",
        "pct_change", "is_st", "is_suspended", "high_limit", "low_limit",
        "adj_factor",
    ])
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "pct_change", "high_limit", "low_limit"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["adj_factor"] = pd.to_numeric(frame["adj_factor"], errors="coerce").fillna(1.0)
    # 前复权（back_adjust_factor 为除权日后全历史统一乘数）
    for column in ("open", "high", "low", "close"):
        frame[column] = frame[column] * frame["adj_factor"]
    frame = frame.drop(columns=["adj_factor"])
    frame = frame.drop_duplicates(["date", "code"], keep="last")
    return frame


def _load_index_close(
    *, date_start: str, date_end: str, index_symbol: str = "000001.SH",
) -> pd.Series:
    """加载上证指数收盘价序列（用于 MA20 过滤）。"""
    rows = client().query(
        """
        SELECT toDate(trade_time), toFloat64(close)
        FROM starlight.ad_market_kline_daily
        WHERE code = {code:String}
          AND toDate(trade_time) >= {date_start:Date} - INTERVAL 60 DAY
          AND toDate(trade_time) <= {date_end:Date}
          AND close IS NOT NULL AND close > 0
        ORDER BY trade_time
        """,
        parameters={
            "code": index_symbol,
            "date_start": date.fromisoformat(str(date_start)[:10]),
            "date_end": date.fromisoformat(str(date_end)[:10]),
        },
    ).result_rows
    if not rows:
        return pd.Series(dtype=float)
    series = pd.Series(
        [float(r[1]) for r in rows],
        index=pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows]),
        dtype=float,
    )
    return series[~series.index.duplicated(keep="last")].sort_index()


def _load_industry_frame(
    frame: pd.DataFrame,
) -> pd.DataFrame:
    """为预测面板补申万 L1 行业（PIT 安全历史成分）。"""
    if frame.empty:
        return frame.assign(industry="")
    observations = frame[["trade_date", "instrument"]].copy()
    date_start = observations["trade_date"].min().date()
    date_end = observations["trade_date"].max().date()
    mapping = _historical_industry_mapping(
        observations=observations, date_start=date_start, date_end=date_end,
    )
    if mapping.empty:
        return frame.assign(industry="")
    mapping["trade_date"] = pd.to_datetime(mapping["trade_date"], errors="coerce")
    result = frame.drop(columns=["industry"], errors="ignore").merge(
        mapping, on=["trade_date", "instrument"], how="left",
    )
    result["industry"] = result["industry"].fillna("")
    return result


def _load_market_cap_map(
    instruments: list[str], trade_date: str,
) -> dict[str, float]:
    """加载指定交易日的总市值（元），PIT 安全。"""
    if not instruments:
        return {}
    cap_frame = _historical_market_cap_frame(
        instruments=sorted(set(instruments)),
        date_start=date.fromisoformat(str(trade_date)[:10]),
        date_end=date.fromisoformat(str(trade_date)[:10]),
    )
    if cap_frame.empty:
        return {}
    cap_frame = cap_frame[cap_frame["trade_date"] <= pd.Timestamp(trade_date)]
    if cap_frame.empty:
        return {}
    latest = cap_frame.sort_values("trade_date").groupby("instrument").tail(1)
    return {
        str(row.instrument): float(row.market_cap)
        for row in latest.itertuples(index=False)
        if np.isfinite(row.market_cap)
    }


def _load_stock_names(instruments: list[str]) -> dict[str, str]:
    if not instruments:
        return {}
    names: dict[str, str] = {}
    for chunk_start in range(0, len(instruments), 500):
        chunk = instruments[chunk_start:chunk_start + 500]
        rows = client().query(
            """
            SELECT market_code,
                   argMax(coalesce(security_name, comp_name, ''), snapshot_date)
            FROM starlight.ad_stock_basic
            WHERE market_code IN {codes:Array(String)}
            GROUP BY market_code
            """,
            parameters={"codes": chunk},
        ).result_rows
        for code, name in rows:
            if name:
                names[str(code)] = str(name)
    return names


# ---------------------------------------------------------------------------
# 行业信号 / 市场状态（与 QuantMind 一致）
# ---------------------------------------------------------------------------

def _compute_industry_signals(
    day_scores: pd.DataFrame,
    industry_map: dict[str, str],
) -> tuple[dict[str, float], dict[str, int], float, int]:
    if day_scores.empty:
        return {}, {}, 0.0, 0
    joined = day_scores.copy()
    joined["industry"] = joined["symbol"].map(industry_map)
    joined = joined[joined["industry"].notna() & (joined["industry"] != "")]
    if joined.empty:
        return {}, {}, 0.0, 0
    ind_top1 = (
        joined.sort_values("score", ascending=False)
        .groupby("industry")
        .first()["score"]
        .to_dict()
    )
    ind_count = joined.groupby("industry")["score"].count().to_dict()
    top20 = joined.nlargest(20, "score")
    top20_industries = top20["industry"].unique()
    if len(top20_industries) > 0:
        avg_top1 = float(np.mean([ind_top1[i] for i in top20_industries if i in ind_top1]))
    else:
        avg_top1 = 0.0
    strong = sum(1 for v in ind_top1.values() if v >= _INDUSTRY_STRONG_SCORE)
    return ind_top1, ind_count, avg_top1, strong


def _market_state(avg_top1: float, strong_count: int) -> str:
    if avg_top1 >= 0.12:
        return "牛市"
    if avg_top1 >= 0.10:
        return "震荡偏强"
    if avg_top1 >= 0.09:
        return "震荡"
    if avg_top1 >= 0.06:
        return "震荡偏弱"
    return "熊市"


# ---------------------------------------------------------------------------
# 股票过滤（与 QuantMind 一致）
# ---------------------------------------------------------------------------

def _is_main_board(symbol: str) -> bool:
    s = symbol.split(".")[0] if "." in symbol else symbol
    return s.startswith(("600", "601", "603", "605", "000", "001", "002"))


def _is_star_market(symbol: str) -> bool:
    s = symbol.split(".")[0] if "." in symbol else symbol
    return s.startswith(("688", "300", "301"))


def _board_type(symbol: str) -> str:
    s = symbol.split(".")[0] if "." in symbol else symbol
    if s.startswith("688"):
        return "科创板"
    if s.startswith(("300", "301")):
        return "创业板"
    if s.startswith(("43", "83", "87", "88", "92")) and len(s) == 6:
        return "北交所"
    if s.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return "沪深主板"
    return "其他"


def _cap_bucket(total_mv: float | None) -> str:
    if total_mv is None:
        return "未知"
    if total_mv < 3e9:
        return "微盘"
    if total_mv < 1e10:
        return "小盘"
    if total_mv < 3e10:
        return "中盘"
    if total_mv < 1e11:
        return "大盘"
    return "超大盘"


def _short_signal(score: float, cap: str) -> tuple[bool, str]:
    if score >= -0.06:
        return False, "轻负分(>-0.06)无信息"
    if cap in ("大盘", "超大盘"):
        if score <= -0.22:
            return True, "超大盘跌破警戒线-0.22，大盘股也会崩"
        return False, "大盘/超大盘负分是错杀"
    return True, "负分可做空"


def _missed_opportunity(score: float, cap: str, board: str) -> bool:
    if score >= -0.06:
        return False
    if cap == "超大盘" and -0.14 <= score <= -0.13:
        return True
    if cap == "大盘" and -0.115 <= score <= -0.105:
        return True
    if board == "科创板" and score >= -0.15:
        return True
    return False


def _check_three_day_trend(
    score_t_minus_1: float | None,
    score_t: float,
    score_t_plus_1: float | None,
) -> tuple[bool, str]:
    if score_t is None:
        return False, "无今日分数"
    if score_t_minus_1 is not None and score_t_plus_1 is not None:
        if score_t_minus_1 < score_t and score_t_plus_1 < score_t:
            return True, "先升后降"
        if score_t_minus_1 < score_t <= score_t_plus_1:
            return False, "连续上升"
        if score_t_minus_1 >= score_t > score_t_plus_1:
            return True, "回落中"
        return False, "连续下降"
    if score_t_minus_1 is not None:
        if score_t_minus_1 < score_t:
            return True, "上升中"
        return True, "回落中"
    if score_t_plus_1 is not None:
        if score_t_plus_1 < score_t:
            return True, "明日回落"
        return False, "连续上升"
    return True, "趋势未知"


def _select_stocks_daily(
    day_scores: pd.DataFrame,
    industry_map: dict[str, str],
    config: StrategyConfig,
    price_day: pd.DataFrame | None,
    history_scores: dict[str, dict[str, float] | None] | None = None,
) -> list[dict[str, Any]]:
    """按策略单日选股。day_scores: DataFrame[symbol, score]（已去重）。"""
    if day_scores.empty:
        return []
    df = day_scores.copy()
    df["symbol"] = df["symbol"].astype(str)
    df["industry"] = df["symbol"].map(industry_map)

    df = df[(df["score"] >= config.score_min) & (df["score"] <= config.score_max)]
    if config.main_board_only:
        df = df[df["symbol"].apply(_is_main_board)]
    df = df[~df["symbol"].apply(_is_star_market)]

    if price_day is not None and not price_day.empty:
        price_map = price_day.set_index("symbol")
        has_st_col = "is_st" in price_day.columns
        keep = []
        for _, row in df.iterrows():
            p = price_map.loc[row["symbol"]] if row["symbol"] in price_map.index else None
            if p is None:
                keep.append(True)
                continue
            if config.exclude_st and has_st_col and pd.notna(p.get("is_st")) and float(p["is_st"]) == 1:
                keep.append(False)
                continue
            if config.exclude_limit_moves and pd.notna(p.get("pct_change")):
                pct = float(p["pct_change"])
                if abs(pct) >= 9.8:
                    keep.append(False)
                    continue
            keep.append(True)
        df = df[keep]

    if "industry" not in df.columns:
        df["industry"] = ""
    df = df[df["industry"].notna() & (df["industry"] != "")]

    trend_map: dict[str, str] = {}
    if history_scores:
        keep = []
        for row in df.itertuples(index=False):
            hist = history_scores.get(row.symbol)
            if hist is None:
                keep.append(True)
                continue
            ok, trend = _check_three_day_trend(
                hist.get("score_t_minus_1"), float(row.score), hist.get("score_t_plus_1")
            )
            trend_map[row.symbol] = trend
            keep.append(ok)
        df = df[keep]

    if df.empty:
        return []
    df = df.sort_values("score", ascending=False).head(config.daily_select_max)
    return [
        {
            "symbol": str(r.symbol),
            "score": float(r.score),
            "industry": str(r.industry),
            "trend": trend_map.get(str(r.symbol), "趋势未知"),
        }
        for r in df.itertuples(index=False)
    ]


# ---------------------------------------------------------------------------
# 事件驱动模拟引擎（与 QuantMind 一致）
# ---------------------------------------------------------------------------

class _SimulationEngine:
    def __init__(self, config: StrategyConfig, industry_map: dict[str, str]):
        self.config = config
        self.industry_map = industry_map
        self.cash = config.initial_capital
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.daily_selections: list[DailySelection] = []
        self.nav_history: list[dict[str, Any]] = []
        self.holdings_snapshot: list[dict[str, Any]] = []
        self.index_close: pd.Series = pd.Series(dtype=float)
        self.index_ma20: pd.Series = pd.Series(dtype=float)
        self.price_panel: pd.DataFrame = pd.DataFrame()
        self.trade_dates_sorted: list[str] = []
        self.date_pos: dict[str, int] = {}

    def setup_prices(self, panel: pd.DataFrame, index_series: pd.Series) -> None:
        self.price_panel = panel
        self.index_close = index_series
        if not index_series.empty:
            self.index_ma20 = index_series.rolling(20).mean()
        self.trade_dates_sorted = (
            sorted({pd.Timestamp(d).strftime("%Y-%m-%d") for d in panel["date"]})
            if not panel.empty else []
        )
        self.date_pos = {d: i for i, d in enumerate(self.trade_dates_sorted)}

    def _next_date(self, trade_date: str) -> str | None:
        idx = self.date_pos.get(trade_date)
        if idx is None or idx + 1 >= len(self.trade_dates_sorted):
            return None
        return self.trade_dates_sorted[idx + 1]

    def _get_prices(self, symbol: str, trade_date: str) -> dict[str, float] | None:
        if self.price_panel.empty:
            return None
        key_date = pd.Timestamp(trade_date)
        row = self.price_panel[
            (self.price_panel["code"] == symbol) & (self.price_panel["date"] == key_date)
        ]
        if row.empty:
            return None
        r = row.iloc[0]
        return {
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        }

    def _index_above_ma20(self, trade_date: str) -> bool:
        if self.index_close.empty or self.index_ma20.empty:
            return True
        key = pd.Timestamp(trade_date)
        if key not in self.index_close.index:
            return True
        close = self.index_close[key]
        ma = self.index_ma20[key]
        if pd.isna(ma):
            return True
        return float(close) >= float(ma)

    def _price_day_frame(self, trade_date: str) -> pd.DataFrame:
        if self.price_panel.empty:
            return pd.DataFrame()
        key_date = pd.Timestamp(trade_date)
        return self.price_panel[self.price_panel["date"] == key_date].copy()

    def run_day(self, trade_date: str, day_scores: pd.DataFrame) -> None:
        if not day_scores.empty and "symbol" in day_scores.columns:
            day_scores = day_scores.copy()
            day_scores["symbol"] = day_scores["symbol"].astype(str)
        price_day = self._price_day_frame(trade_date)
        if not price_day.empty:
            price_day = price_day.rename(columns={"code": "symbol"})

        index_ok = self._index_above_ma20(trade_date)
        ind_top1, ind_count, avg_top1, strong_count = _compute_industry_signals(
            day_scores, self.industry_map
        )
        state = _market_state(avg_top1, strong_count)

        self._process_sells(trade_date, avg_top1, ind_top1, index_ok)

        should_enter = (
            index_ok
            and avg_top1 >= self.config.entry_threshold
            and strong_count >= self.config.strong_industry_min
        )
        if not should_enter:
            self.daily_selections.append(DailySelection(
                trade_date=trade_date,
                market_state=state,
                industry_avg_top1=avg_top1,
                strong_industry_count=strong_count,
                index_above_ma20=index_ok,
                selections=[],
            ))
            self._record_nav(trade_date)
            return

        picks = _select_stocks_daily(day_scores, self.industry_map, self.config, price_day)
        exec_date = self._next_date(trade_date)
        if exec_date:
            self._process_buys(picks, exec_date)

        self.daily_selections.append(DailySelection(
            trade_date=trade_date,
            market_state=state,
            industry_avg_top1=avg_top1,
            strong_industry_count=strong_count,
            index_above_ma20=index_ok,
            selections=picks,
        ))
        self._record_nav(trade_date)

    # -- 卖出 --

    def _process_sells(
        self,
        trade_date: str,
        avg_top1: float,
        ind_top1: dict[str, float],
        index_ok: bool,
    ) -> None:
        to_sell: list[tuple[str, str]] = []
        for symbol, pos in list(self.positions.items()):
            price = self._get_prices(symbol, trade_date)
            if price is None:
                continue
            close = price["close"]
            pos.hold_days += 1
            pos.open_pnl = (close / pos.buy_price - 1.0)
            if pos.hold_days >= self.config.max_hold_days:
                to_sell.append((symbol, "持有到期"))
                continue
            if pos.open_pnl >= self.config.take_profit:
                to_sell.append((symbol, "止盈"))
                continue
            if pos.open_pnl <= -self.config.stop_loss:
                if not self._is_limit_down(symbol, trade_date):
                    to_sell.append((symbol, "止损"))
                continue
            ind = pos.industry
            if ind in ind_top1 and ind_top1[ind] < self.config.entry_threshold:
                to_sell.append((symbol, "行业转弱"))
                continue
            if not index_ok and self.config.use_index_ma20_filter:
                to_sell.append((symbol, "大盘MA20"))

        for symbol, reason in to_sell:
            pos = self.positions.get(symbol)
            if pos is not None:
                self._execute_sell(pos, trade_date, reason)

    def _is_limit_down(self, symbol: str, trade_date: str) -> bool:
        if self.price_panel.empty:
            return False
        key_date = pd.Timestamp(trade_date)
        row = self.price_panel[
            (self.price_panel["code"] == symbol) & (self.price_panel["date"] == key_date)
        ]
        if row.empty or "pct_change" not in row.columns:
            return False
        pct = row.iloc[0].get("pct_change")
        return pct is not None and pd.notna(pct) and float(pct) <= -9.8

    def _execute_sell(self, pos: Position, trade_date: str, reason: str) -> None:
        price = self._get_prices(pos.symbol, trade_date)
        if price is None:
            return
        sell_price = price["close"]
        amount = sell_price * pos.shares
        cost = amount * (0.001 + 0.00025)
        self.cash += amount - cost
        pos.sell_date = trade_date
        pos.sell_price = sell_price
        pos.sell_reason = reason
        pos.profit_pct = sell_price / pos.buy_price - 1.0
        pos.realized_pnl = (sell_price - pos.buy_price) * pos.shares - cost
        self.trades.append(Trade(
            date=trade_date,
            symbol=pos.symbol,
            name=pos.name,
            side="SELL",
            price=sell_price,
            shares=pos.shares,
            amount=amount,
            industry=pos.industry,
            score=pos.score,
            reason=reason,
            profit_pct=pos.profit_pct,
            hold_days=pos.hold_days,
        ))
        del self.positions[pos.symbol]

    # -- 买入 --

    def _process_buys(self, picks: list[dict[str, Any]], exec_date: str) -> None:
        slots = self.config.max_positions - len(self.positions)
        if slots <= 0:
            return
        for pick in picks[:slots]:
            symbol = pick["symbol"]
            price = self._get_prices(symbol, exec_date)
            if price is None:
                continue
            buy_price = price["open"]
            if buy_price <= 0:
                continue
            alloc = self.cash / max(1, slots)
            shares = int(alloc / buy_price / 100) * 100
            if shares <= 0:
                continue
            amount = shares * buy_price
            cost = amount * 0.00025
            if amount + cost > self.cash:
                shares = int((self.cash - cost) / buy_price / 100) * 100
                if shares <= 0:
                    continue
                amount = shares * buy_price
                cost = amount * 0.00025
            self.cash -= (amount + cost)
            pos = Position(
                symbol=symbol,
                name="",
                industry=pick["industry"],
                score=pick["score"],
                buy_date=exec_date,
                buy_price=buy_price,
                shares=shares,
            )
            self.positions[symbol] = pos
            self.trades.append(Trade(
                date=exec_date,
                symbol=symbol,
                name="",
                side="BUY",
                price=buy_price,
                shares=shares,
                amount=amount,
                industry=pick["industry"],
                score=pick["score"],
                reason="策略选股",
            ))

    # -- 净值 --

    def _record_nav(self, trade_date: str) -> None:
        if self.price_panel.empty:
            self.nav_history.append({
                "date": trade_date, "nav": self.cash, "cash": self.cash,
                "holdings": 0.0, "position_count": 0,
            })
            self.holdings_snapshot.append({
                "date": trade_date, "holdings": [], "cash": self.cash,
            })
            return
        holdings_value = 0.0
        holdings_rows: list[dict[str, Any]] = []
        for symbol, pos in self.positions.items():
            price = self._get_prices(symbol, trade_date)
            if price is None:
                continue
            value = price["close"] * pos.shares
            holdings_value += value
            holdings_rows.append({
                "symbol": symbol, "name": pos.name, "industry": pos.industry,
                "score": pos.score, "shares": pos.shares,
                "buy_price": pos.buy_price, "close": price["close"],
                "value": round(value, 2), "profit_pct": round(price["close"] / pos.buy_price - 1.0, 4),
            })
        nav = self.cash + holdings_value
        peak = max((n["nav"] for n in self.nav_history), default=nav)
        self.nav_history.append({
            "date": trade_date,
            "nav": round(float(nav), 2),
            "cash": round(float(self.cash), 2),
            "holdings": round(float(holdings_value), 2),
            "position_count": len(holdings_rows),
            "drawdown": round(float((peak - nav) / peak), 4) if peak > 0 else 0.0,
        })
        self.holdings_snapshot.append({
            "date": trade_date, "holdings": holdings_rows,
            "cash": round(float(self.cash), 2),
        })

    def finalize(self) -> None:
        # 回测末尾仍未卖出的持仓按最后一日收盘价强制了结（只记录，不改现金）
        if not self.nav_history:
            return
        last_date = self.nav_history[-1]["date"]
        for symbol, pos in list(self.positions.items()):
            price = self._get_prices(symbol, last_date)
            if price is None:
                continue
            pos.sell_date = last_date
            pos.sell_price = price["close"]
            pos.sell_reason = "回测结束"
            pos.profit_pct = price["close"] / pos.buy_price - 1.0


# ---------------------------------------------------------------------------
# 指标计算（与 QuantMind 一致）
# ---------------------------------------------------------------------------

def _compute_metrics(
    nav_history: list[dict[str, Any]],
    trades: list[Trade],
    config: StrategyConfig,
) -> dict[str, Any]:
    if not nav_history:
        return {"total_return": 0.0, "max_drawdown": 0.0, "trade_count": 0}
    navs = [n["nav"] for n in nav_history]
    start_nav = config.initial_capital
    end_nav = navs[-1]
    total_return = end_nav / start_nav - 1.0
    days = max(1, len(nav_history))
    years = days / 252.0
    annualized = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1.0
    peak = navs[0]
    max_dd = 0.0
    for n in navs:
        peak = max(peak, n)
        dd = (peak - n) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    sells = [t for t in trades if t.side == "SELL"]
    wins = [t for t in sells if t.profit_pct > 0]
    win_rate = len(wins) / len(sells) if sells else 0.0
    profits = [t.profit_pct for t in sells if t.profit_pct > 0]
    losses = [t.profit_pct for t in sells if t.profit_pct <= 0]
    avg_profit = float(np.mean(profits)) if profits else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    ret_dd_ratio = total_return / max_dd if max_dd > 0 else 0.0
    return {
        "initial_capital": config.initial_capital,
        "final_nav": round(float(end_nav), 2),
        "total_return": round(float(total_return), 4),
        "annualized_return": round(float(annualized), 4),
        "max_drawdown": round(float(max_dd), 4),
        "win_rate": round(float(win_rate), 4),
        "trade_count": len(trades),
        "buy_count": len([t for t in trades if t.side == "BUY"]),
        "sell_count": len(sells),
        "avg_profit": round(float(avg_profit), 4),
        "avg_loss": round(float(avg_loss), 4),
        "ret_dd_ratio": round(float(ret_dd_ratio), 4),
        "position_days": len(nav_history),
        "empty_days": sum(1 for n in nav_history if n["position_count"] == 0),
    }


def _compute_monthly_returns(nav_history: list[dict[str, Any]]) -> dict[str, float]:
    monthly_nav: dict[str, float] = {}
    for n in nav_history:
        month = str(n["date"])[:7]
        monthly_nav[month] = n["nav"]
    monthly_returns: dict[str, float] = {}
    months = sorted(monthly_nav)
    for i, month in enumerate(months):
        if i == 0:
            continue
        prev_nav = monthly_nav[months[i - 1]]
        cur_nav = monthly_nav[month]
        if prev_nav > 0:
            monthly_returns[month] = round(cur_nav / prev_nav - 1.0, 4)
    return monthly_returns


def _compute_industry_rotation(
    daily_selections: list[DailySelection],
) -> list[dict[str, Any]]:
    month_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for sel in daily_selections:
        if sel.market_state in ("熊市", "震荡偏弱"):
            continue
        month = sel.trade_date[:7]
        for pick in sel.selections:
            month_counts[month][pick["industry"]] += 1
    result: list[dict[str, Any]] = []
    for month in sorted(month_counts):
        top = sorted(month_counts[month].items(), key=lambda x: -x[1])[:5]
        result.append({
            "month": month,
            "top_industries": [{"industry": k, "days": v} for k, v in top],
        })
    return result


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def daily_selection(
    *, model_id: str, model_version: int, strategy: str = "balanced",
    trade_date: str | None = None, ignore_ma20: bool = False,
) -> dict[str, Any]:
    """今日选股：市场状态 + 行业排行 + 候选股 + 被排除示例。"""
    cfg = StrategyConfig.preset(strategy)

    resolved_date = trade_date or _latest_prediction_date(
        model_id=model_id, model_version=model_version,
    )
    if not resolved_date:
        return {
            "status": "success",
            "meta": {"trade_date": None, "strategy": strategy, "total_signals": 0},
            "market_state": {"state": "无信号", "should_enter": False, "position_advice": "0%"},
            "industry_signals": [],
            "candidates": [],
            "excluded_examples": [],
            "warnings": [f"无推理信号（model={model_id} v{model_version}）"],
        }
    resolved_date = str(resolved_date)[:10]

    frame = _load_prediction_panel(
        model_id=model_id, model_version=model_version, trade_date=resolved_date,
    )
    if frame.empty:
        return {
            "status": "success",
            "meta": {"trade_date": resolved_date, "strategy": strategy, "total_signals": 0},
            "market_state": {"state": "无信号", "should_enter": False, "position_advice": "0%"},
            "industry_signals": [],
            "candidates": [],
            "excluded_examples": [],
            "warnings": [f"无推理信号（model={model_id} v{model_version}, date={resolved_date}）"],
        }

    # 3 天趋势需要 T-1 / T+1 分数：加载目标日前后约两周的预测窗口
    target = date.fromisoformat(resolved_date)
    history_window = _load_prediction_panel(
        model_id=model_id, model_version=model_version,
        date_start=str(target - timedelta(days=14)),
        date_end=str(target + timedelta(days=14)),
    )
    if not history_window.empty:
        history_window = _load_industry_frame(history_window)
        frame = history_window[history_window["trade_date"] == pd.Timestamp(target)].copy()
        if frame.empty:
            frame = history_window.iloc[0:0].copy()

    frame = _load_industry_frame(frame)
    day_scores = frame.rename(columns={"instrument": "symbol"})[
        ["trade_date", "symbol", "score", "industry"]
    ]
    day_scores["symbol"] = day_scores["symbol"].astype(str)
    day_scores = day_scores.drop_duplicates(subset="symbol", keep="last")

    # 行业信号
    industry_map = {
        str(row.symbol): str(row.industry)
        for row in day_scores.itertuples(index=False)
        if row.industry
    }
    ind_top1, ind_count, avg_top1, strong_count = _compute_industry_signals(
        day_scores, industry_map,
    )
    state = _market_state(avg_top1, strong_count)
    index_above_ma20, index_detail = _index_ma20_status(resolved_date)
    position = _position_advice(avg_top1, strong_count)

    ma20_ok = index_above_ma20 or ignore_ma20
    should_enter = (
        ma20_ok
        and avg_top1 >= cfg.entry_threshold
        and strong_count >= cfg.strong_industry_min
    )
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    if should_enter:
        price_day = _price_flags_for_date(day_scores, resolved_date)
        history_scores = _three_day_history(history_window, day_scores, resolved_date)
        picks = _select_stocks_daily(
            day_scores, industry_map, cfg, price_day, history_scores,
        )
        candidates = picks
    else:
        reason = []
        if not index_above_ma20 and not ignore_ma20:
            reason.append(f"大盘跌破MA20（{index_detail}）")
        if avg_top1 < cfg.entry_threshold:
            reason.append(f"行业avgTop1={avg_top1:.3f} 低于入场线{cfg.entry_threshold}")
        if strong_count < cfg.strong_industry_min:
            reason.append(f"强行业数{strong_count} 低于阈值{cfg.strong_industry_min}")
        excluded.append({
            "symbol": "", "score": 0, "reason": "未入场",
            "detail": "；".join(reason) or "市场状态不满足入场条件",
        })

    # 行业排行（Top15）
    top_stock_by_ind = _top_stock_by_industry(day_scores, industry_map)
    industry_signals = sorted(
        [{"industry": i, "top1": v, "stock": top_stock_by_ind.get(i, "")}
         for i, v in ind_top1.items()],
        key=lambda x: -x["top1"],
    )[:15]

    # 股票名称补齐
    name_map = _load_stock_names([c["symbol"] for c in candidates])
    for c in candidates:
        c["name"] = name_map.get(c["symbol"], "")
        reasons = ["黄金区间" if cfg.score_min <= c["score"] <= cfg.score_max else "分数区间"]
        if c.get("trend") in ("先升后降", "上升中", "明日回落"):
            reasons.append("先升后降")
        if _is_main_board(c["symbol"]):
            reasons.append("主板")
        ind = c.get("industry", "")
        if ind in ind_top1 and ind_top1[ind] >= cfg.entry_threshold:
            reasons.append("行业确认")
        c["buy_reason"] = "+".join(reasons)
        c["warnings"] = []

    return {
        "status": "success",
        "meta": {
            "trade_date": resolved_date,
            "strategy": strategy,
            "total_signals": len(day_scores),
            "strategy_config": {
                "entry_threshold": cfg.entry_threshold,
                "exit_threshold": cfg.exit_threshold,
                "strong_industry_min": cfg.strong_industry_min,
                "score_min": cfg.score_min,
                "score_max": cfg.score_max,
                "max_positions": cfg.max_positions,
            },
        },
        "market_state": {
            "state": state,
            "avg_top1": round(float(avg_top1), 4),
            "strong_count": int(strong_count),
            "index_above_ma20": bool(index_above_ma20),
            "index_detail": index_detail,
            "ignore_ma20": bool(ignore_ma20),
            "should_enter": bool(should_enter),
            "position": position["position"],
            "position_reason": position["reason"],
        },
        "industry_signals": industry_signals,
        "candidates": candidates,
        "excluded_examples": excluded,
        "warnings": [],
    }


def negative_selection(
    *, model_id: str, model_version: int, trade_date: str | None = None,
) -> dict[str, Any]:
    """负分多空参考：做空候选 + 错杀参考 + 分数×市值分布矩阵。"""
    resolved_date = trade_date or _latest_prediction_date(
        model_id=model_id, model_version=model_version,
    )
    if not resolved_date:
        return {
            "status": "success",
            "meta": {"trade_date": None, "total_signals": 0},
            "short_candidates": [], "missed_reference": [],
            "matrix": [], "warnings": ["无推理信号"],
        }
    resolved_date = str(resolved_date)[:10]
    frame = _load_prediction_panel(
        model_id=model_id, model_version=model_version, trade_date=resolved_date,
    )
    if frame.empty:
        return {
            "status": "success",
            "meta": {"trade_date": resolved_date, "total_signals": 0},
            "short_candidates": [], "missed_reference": [],
            "matrix": [], "warnings": ["无推理信号"],
        }

    day_scores = frame.rename(columns={"instrument": "symbol"})[
        ["symbol", "score"]
    ].drop_duplicates(subset="symbol", keep="last")
    neg_df = day_scores[day_scores["score"] < -0.06].copy()
    neg_df = neg_df.sort_values("score")

    all_symbols = neg_df["symbol"].astype(str).tolist()
    caps = _load_market_cap_map(all_symbols, resolved_date)
    names = _load_stock_names(all_symbols)
    neg_df["cap"] = neg_df["symbol"].map(lambda s: _cap_bucket(caps.get(str(s))))
    neg_df["board"] = neg_df["symbol"].map(_board_type)
    neg_df["name"] = neg_df["symbol"].map(names)

    short_candidates: list[dict[str, Any]] = []
    missed_reference: list[dict[str, Any]] = []
    for row in neg_df.itertuples(index=False):
        item = {
            "symbol": str(row.symbol),
            "name": str(row.name or ""),
            "score": round(float(row.score), 4),
            "cap": str(row.cap),
            "board": str(row.board),
        }
        do_short, reason = _short_signal(float(row.score), str(row.cap))
        if do_short:
            item["short_reason"] = reason
            short_candidates.append(item)
        if _missed_opportunity(float(row.score), str(row.cap), str(row.board)):
            item["missed_reason"] = "负分错杀，可能反弹"
            missed_reference.append(item)

    cap_order = {"微盘": 0, "小盘": 1, "中盘": 2, "大盘": 3, "超大盘": 4, "未知": 5}
    short_candidates.sort(key=lambda x: (cap_order.get(x["cap"], 5), x["score"]))

    matrix: list[dict[str, Any]] = []
    score_bands = [
        ("≤-0.25", lambda s: s <= -0.25),
        ("-0.25~-0.20", lambda s: -0.25 < s <= -0.20),
        ("-0.20~-0.15", lambda s: -0.20 < s <= -0.15),
        ("-0.15~-0.10", lambda s: -0.15 < s <= -0.10),
        ("-0.10~-0.06", lambda s: -0.10 < s <= -0.06),
    ]
    cap_buckets = ["微盘", "小盘", "中盘", "大盘", "超大盘"]
    for band_label, band_fn in score_bands:
        row_entries: list[dict[str, Any]] = []
        for cap_label in cap_buckets:
            count = int(((neg_df["score"].map(band_fn)) & (neg_df["cap"] == cap_label)).sum())
            row_entries.append({"cap": cap_label, "count": count})
        matrix.append({"score_band": band_label, "caps": row_entries})

    return {
        "status": "success",
        "meta": {
            "trade_date": resolved_date,
            "total_signals": len(day_scores),
            "negative_count": len(neg_df),
        },
        "short_candidates": short_candidates[:30],
        "missed_reference": missed_reference[:20],
        "matrix": matrix,
        "warnings": [],
    }


def run_inference_backtest(
    *, model_id: str, model_version: int, start_date: str, end_date: str,
    strategy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """事件驱动推理回测（stored 信号：读已有模型预测）。"""
    cfg = StrategyConfig.from_mapping(strategy)

    predictions = _load_prediction_panel(
        model_id=model_id, model_version=model_version,
        date_start=start_date, date_end=end_date,
    )
    if predictions.empty:
        return {
            "status": "success",
            "metrics": _compute_metrics([], [], cfg),
            "daily_selections": [], "trades": [], "nav_curve": [],
            "holdings_snapshot": [], "industry_rotation": [],
            "monthly_returns": {},
            "errors": [{"date": "", "error": "回测区间内无PIT安全预测信号"}],
            "warnings": [],
        }

    predictions = _load_industry_frame(predictions)
    signal_dates = sorted(predictions["trade_date"].dt.strftime("%Y-%m-%d").unique())
    codes = sorted(predictions["instrument"].astype(str).unique())
    price_panel = _load_price_panel(
        codes=codes, date_start=start_date, date_end=end_date,
    )
    index_close = _load_index_close(
        date_start=start_date, date_end=end_date,
        index_symbol=cfg.index_symbol,
    )
    industry_map = {
        str(row.instrument): str(row.industry)
        for row in predictions[predictions["industry"] != ""].itertuples(index=False)
    }

    engine = _SimulationEngine(cfg, industry_map)
    engine.setup_prices(price_panel, index_close)

    # 完整交易日历推进：无信号日传空表，仍然执行卖出/净值/大盘MA20判断
    calendar = [
        d for d in engine.trade_dates_sorted
        if start_date <= d <= end_date
    ]

    errors: list[dict[str, str]] = []
    date_to_scores: dict[str, pd.DataFrame] = {}
    for trade_date, group in predictions.groupby(
        predictions["trade_date"].dt.strftime("%Y-%m-%d"), sort=True,
    ):
        date_to_scores[trade_date] = group.rename(columns={"instrument": "symbol"})[
            ["symbol", "score"]
        ].drop_duplicates(subset="symbol", keep="last")

    for trade_date in calendar:
        try:
            day_scores = date_to_scores.get(
                trade_date, pd.DataFrame(columns=["symbol", "score"]),
            )
            engine.run_day(trade_date, day_scores)
        except Exception as exc:
            logger.warning("回测日期 %s 处理失败: %s", trade_date, exc)
            errors.append({"date": trade_date, "error": str(exc)})

    engine.finalize()
    metrics = _compute_metrics(engine.nav_history, engine.trades, cfg)
    monthly = _compute_monthly_returns(engine.nav_history)
    rotation = _compute_industry_rotation(engine.daily_selections)

    return {
        "status": "success",
        "metrics": metrics,
        "daily_selections": [
            {
                "trade_date": sel.trade_date,
                "market_state": sel.market_state,
                "industry_avg_top1": round(float(sel.industry_avg_top1), 4),
                "strong_industry_count": int(sel.strong_industry_count),
                "index_above_ma20": bool(sel.index_above_ma20),
                "selections": sel.selections,
            }
            for sel in engine.daily_selections
        ],
        "trades": [
            {
                "date": t.date,
                "symbol": t.symbol,
                "name": t.name,
                "side": t.side,
                "price": round(float(t.price), 4),
                "shares": int(t.shares),
                "amount": round(float(t.amount), 2),
                "industry": t.industry,
                "score": round(float(t.score), 4),
                "reason": t.reason,
                "profit_pct": round(float(t.profit_pct), 4),
                "hold_days": int(t.hold_days),
            }
            for t in engine.trades
        ],
        "nav_curve": engine.nav_history,
        "holdings_snapshot": engine.holdings_snapshot,
        "industry_rotation": rotation,
        "monthly_returns": monthly,
        "errors": errors,
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _position_advice(avg_top1: float, strong_count: int) -> dict[str, str]:
    if avg_top1 >= 0.12 and strong_count >= 5:
        return {"position": "100%", "reason": "牛市，满仓可追强信号"}
    if avg_top1 >= 0.10 and strong_count >= 3:
        return {"position": "50%", "reason": "震荡偏强，半仓只做强区间"}
    if avg_top1 >= 0.09 and strong_count >= 2:
        return {"position": "30%", "reason": "震荡，轻仓快进快出"}
    if avg_top1 >= 0.06:
        return {"position": "0-30%", "reason": "震荡偏弱，观望或极轻仓"}
    return {"position": "0%", "reason": "熊市，绝对空仓"}


def _index_ma20_status(trade_date: str) -> tuple[bool, str]:
    """上证指数收盘 vs MA20（用于大盘入场判断）。"""
    try:
        d = date.fromisoformat(str(trade_date)[:10])
        series = _load_index_close(
            date_start=str(d - timedelta(days=60)), date_end=str(d),
        )
        if series.empty:
            return True, "无指数数据"
        key = pd.Timestamp(d)
        up_to = series[series.index <= key]
        if len(up_to) < 20:
            return True, "指数数据不足20日"
        last = float(up_to.iloc[-1])
        ma20 = float(up_to.rolling(20).mean().iloc[-1])
        return (last >= ma20, f"上证{last:.0f}/MA20{ma20:.0f}")
    except Exception as exc:
        logger.warning("加载指数 MA20 失败: %s", exc)
        return True, "指数数据不可用"


def _top_stock_by_industry(
    day_scores: pd.DataFrame,
    industry_map: dict[str, str],
) -> dict[str, str]:
    joined = day_scores.copy()
    joined["industry"] = joined["symbol"].map(industry_map)
    joined = joined[joined["industry"].notna() & (joined["industry"] != "")]
    if joined.empty:
        return {}
    idx = joined.groupby("industry")["score"].idxmax()
    return {str(joined.loc[i, "industry"]): str(joined.loc[i, "symbol"]) for i in idx.values}


def _price_flags_for_date(
    day_scores: pd.DataFrame, trade_date: str,
) -> pd.DataFrame:
    """加载指定交易日的价格/ST 标记，供选股过滤使用。"""
    codes = sorted(set(day_scores["symbol"].astype(str)))
    panel = _load_price_panel(
        codes=codes, date_start=trade_date, date_end=trade_date,
    )
    if panel.empty:
        return pd.DataFrame()
    target = pd.Timestamp(trade_date)
    day = panel[panel["date"] == target].copy()
    if day.empty:
        return pd.DataFrame()
    day = day.rename(columns={"code": "symbol"})
    return day[["symbol", "pct_change", "is_st"]]


def _three_day_history(
    frame: pd.DataFrame,
    day_scores: pd.DataFrame,
    trade_date: str,
) -> dict[str, dict[str, float] | None]:
    """为候选股构建 T-1 / T+1 分数（用于 3 天趋势过滤）。"""
    if frame.empty:
        return {}
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    target = pd.Timestamp(trade_date)
    history: dict[str, dict[str, float] | None] = {}
    for symbol in day_scores["symbol"].astype(str).unique():
        rows = frame[frame["instrument"] == symbol]
        if rows.empty:
            history[symbol] = None
            continue
        rows = rows.sort_values("trade_date")
        before = rows[rows["trade_date"] < target]
        after = rows[rows["trade_date"] > target]
        hist: dict[str, float] = {}
        if not before.empty:
            hist["score_t_minus_1"] = float(before.iloc[-1]["score"])
        if not after.empty:
            hist["score_t_plus_1"] = float(after.iloc[0]["score"])
        history[symbol] = hist if hist else None
    return history
