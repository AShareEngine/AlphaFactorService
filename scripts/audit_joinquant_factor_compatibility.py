#!/usr/bin/env python3
"""Audit which JoinQuant catalog factors AlphaFactorService can compute today."""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from factor_service.clickhouse import client
from factor_service.config import load_settings
from factor_service.qlib_formula import FormulaError, compile_qlib_formula


READY = "ready"
REVIEW = "review"
ENGINE = "engine"
MISSING = "missing"


@dataclass(frozen=True)
class CatalogFactor:
    number: int
    name: str
    label: str
    category: str
    processing: str
    logic: str


@dataclass(frozen=True)
class Translation:
    status: str
    expression: str | None
    note: str


@dataclass(frozen=True)
class SourceSnapshot:
    database: str
    table: str
    code_column: str
    date_column: str
    columns: tuple[str, ...]
    coverage: dict[str, float]
    rows: int
    instruments: int
    total_rows: int
    total_instruments: int
    date_start: Any
    date_end: Any


def _mean(field: str, window: int) -> str:
    return f"Mean({field}, {window})"


def _std(field: str, window: int) -> str:
    return f"Std({field}, {window})"


def _period_return(field: str, window: int) -> str:
    return f"PeriodReturn({field}, {window})"


def _ratio(numerator: str, denominator: str) -> str:
    return f"({numerator}) / NullIf(({denominator}), 0)"


def _typical_price() -> str:
    return "(($high + $low + $close) / 3)"


def _atr(window: int) -> str:
    true_range = (
        "Greater(Greater($high - $low, Abs($high - $pre_close)), "
        "Abs($low - $pre_close))"
    )
    return f"Mean({true_range}, {window})"


def _cci(window: int) -> str:
    typical = _typical_price()
    return _ratio(
        f"{typical} - Mean({typical}, {window})",
        f"0.015 * Mad({typical}, {window})",
    )


def _bias(window: int) -> str:
    average = _mean("$close", window)
    return f"{_ratio(f'$close - {average}', average)} * 100"


def _sharpe(window: int) -> str:
    daily_return = "($pct_chg / 100)"
    return _ratio(
        f"Mean({daily_return}, {window}) * 252 - 0.04",
        f"Std({daily_return}, {window}) * Power(252, 0.5)",
    )


def build_translations() -> dict[str, Translation]:
    translations: dict[str, Translation] = {}

    def ready(name: str, expression: str, note: str = "公式可由当前单表日频引擎直接计算") -> None:
        translations[name] = Translation(READY, expression, note)

    def review(name: str, expression: str, note: str) -> None:
        translations[name] = Translation(REVIEW, expression, note)

    def engine(name: str, note: str) -> None:
        translations[name] = Translation(ENGINE, None, note)

    # 风格因子：当前源中可直接得到的换手率与估值因子。
    ready("average_share_turnover_annual", "Log(Sum($turnover_rate, 252) / 12)")
    ready("average_share_turnover_quarterly", "Log(Sum($turnover_rate, 63) / 3)")
    ready("book_to_price_ratio", "1 / NullIf($pb, 0)")
    ready("earnings_to_price_ratio", "1 / NullIf($pe, 0)")
    ready("share_turnover_monthly", "Log(Sum($turnover_rate, 21))")

    # 情绪类。
    ar_numerator = "Sum(Greater($high - $open, 0), 26)"
    ar_denominator = "Sum(Greater($open - $low, 0), 26)"
    br_numerator = "Sum(Greater($high - $pre_close, 0), 26)"
    br_denominator = "Sum(Greater($pre_close - $low, 0), 26)"
    ar = f"{_ratio(ar_numerator, ar_denominator)} * 100"
    br = f"{_ratio(br_numerator, br_denominator)} * 100"
    ready("AR", ar)
    ready("ARBR", f"({ar}) - ({br})")
    ready("ATR14", _atr(14))
    ready("ATR6", _atr(6))
    ready("BR", br)
    for window in (5, 10, 20):
        ready(
            f"DAVOL{window}",
            _ratio(_mean("$turnover_rate", window), _mean("$turnover_rate", 120)),
        )
    ready("PSY", "Sum(Gt($pct_chg, 0), 12) / 12 * 100")
    ready("turnover_volatility", _std("$turnover_rate", 20))
    for window in (20, 6):
        ready(f"TVMA{window}", _mean("$amount", window))
        ready(f"TVSTD{window}", _std("$amount", window))
    ready("VDIFF", "EMA($volume, 12) - EMA($volume, 26)")
    for window in (5, 10, 20, 60, 120, 240):
        ready(f"VOL{window}", _mean("$turnover_rate", window))
    ready(
        "VOSC",
        _ratio("EMA($volume, 12) - EMA($volume, 26)", "EMA($volume, 12)") + " * 100",
    )
    vr_up = "Sum(If(Gt($pct_chg, 0), $volume, 0), 26)"
    vr_flat = "Sum(If(Eq($pct_chg, 0), $volume, 0), 26)"
    vr_down = "Sum(If(Lt($pct_chg, 0), $volume, 0), 26)"
    ready("VR", _ratio(f"{vr_up} + 0.5 * {vr_flat}", f"{vr_down} + 0.5 * {vr_flat}"))
    for window in (6, 12):
        ready(f"VROC{window}", f"{_period_return('$volume', window)} * 100")
    for window in (10, 20):
        ready(f"VSTD{window}", _std("$volume", window))
    ready(
        "WVAD",
        "Sum((($close - $open) / NullIf($high - $low, 0)) * $volume, 6)",
    )

    # 风险类。pct_chg 的单位为百分数，因此先除以 100。
    for window in (20, 60, 120):
        daily_return = "($pct_chg / 100)"
        ready(f"Kurtosis{window}", f"Kurt({daily_return}, {window})")
        ready(f"sharpe_ratio_{window}", _sharpe(window))
        ready(f"Skewness{window}", f"Skew({daily_return}, {window})")
        ready(f"Variance{window}", f"Var({daily_return}, {window})")

    # 技术指标类。
    ma20 = _mean("$close", 20)
    std20 = _std("$close", 20)
    ready("boll_down", _ratio(f"{ma20} - 2 * {std20}", "$close"))
    ready("boll_up", _ratio(f"{ma20} + 2 * {std20}", "$close"))
    ready("EMA5", _ratio("EMA($close, 5)", "$close"))
    for window in (10, 12, 20, 26, 120):
        ready(f"EMAC{window}", _ratio(f"EMA($close, {window})", "$close"))
    for window in (5, 10, 20, 60, 120):
        ready(f"MAC{window}", _ratio(_mean("$close", window), "$close"))

    # 动量类。
    ready("arron_down_25", "IdxMin($low, 25) / 25 * 100")
    ready("arron_up_25", "IdxMax($high, 25) / 25 * 100")
    ready(
        "BBIC",
        "(Mean($close, 3) + Mean($close, 6) + Mean($close, 12) + Mean($close, 24)) "
        "/ 4 / NullIf($close, 0)",
    )
    ready("bear_power", "($low - EMA($close, 13)) / NullIf($close, 0)")
    ready("bull_power", "($high - EMA($close, 13)) / NullIf($close, 0)")
    for window in (5, 10, 20, 60):
        ready(f"BIAS{window}", _bias(window))
    for window in (10, 15, 20, 88):
        ready(f"CCI{window}", _cci(window))
    for window in (6, 12, 24):
        ready(
            f"PLRC{window}",
            _ratio(f"Slope($close, {window})", f"Mean($close, {window})"),
        )
    for name, window in (("Price1M", 21), ("Price3M", 61), ("Price1Y", 250)):
        ready(name, f"{_ratio('$close', _mean('$close', window))} - 1")
    for window in (6, 12, 20):
        ready(f"ROC{window}", f"{_period_return('$close', window)} * 100")
    vpt = "($pct_chg / 100) * $volume"
    ready("single_day_VPT", vpt)
    ready("single_day_VPT_12", f"Mean({vpt}, 12)")
    ready("single_day_VPT_6", f"Mean({vpt}, 6)")
    ready(
        "Volume1M",
        _ratio("$volume", "Mean($volume, 20)") + " * Mean($pct_chg / 100, 20)",
    )

    # 新风格因子中唯一可由当前估值字段直接映射的因子。
    ready("btop", "1 / NullIf($pb, 0)")

    # 数据和表达式能力基本足够，但聚宽公开说明不足或存在口径冲突，不能自动发布。
    review(
        "money_flow_20",
        f"Mean({_typical_price()} * $volume, 20)",
        "公开说明只定义单日资金流，未说明 20 日使用均值还是求和",
    )
    for window in (5, 10, 12, 26):
        review(
            f"VEMA{window}",
            f"EMA($volume, {window})",
            "聚宽详情没有给出计算逻辑，当前表达式按因子名推定",
        )
    review(
        "price_no_fq",
        "$close",
        "需要先确认 starlight.ad_market_kline_daily.close 是否确为不复权价格",
    )
    review(
        "fifty_two_week_close_rank",
        "1 - Rank($close, 250)",
        "当前时序 Rank 可计算，但聚宽未公开并列值与名次归一化口径",
    )
    for window in (60, 120):
        review(
            f"ROC{window}",
            f"{_period_return('$close', window)} * 100",
            "聚宽公开公式与因子名冲突：两个详情都写成 20 日差值除以 60 日前价格",
        )

    # 当前数据字段已基本够用，但单表达式编译器缺少嵌套窗口、跨截面或特殊加权能力。
    engine("cumulative_range", "需要月频重采样与区间累计收益")
    engine("daily_standard_deviation", "需要半衰期指数加权标准差")
    engine("momentum", "需要跳过最近 21 日并支持半衰期指数权重")
    engine("relative_strength", "需要半衰期指数加权对数收益")
    engine("MAWVAD", "需要先算 WVAD 再做移动平均，当前禁止嵌套窗口")
    engine("VDEA", "需要对 VDIFF 再做 EMA，当前禁止嵌套窗口")
    engine("VMACD", "依赖 VDIFF/VDEA 的多阶段计算")
    engine("MACDC", "需要对 DIF 再做信号线 EMA，当前禁止嵌套窗口")
    engine("MFI14", "需要昨日典型价参与滚动条件求和，当前禁止窗口嵌套")
    engine("CR20", "需要昨日中间价参与滚动求和，当前禁止窗口嵌套")
    engine("CCI88", "现有 Mad 展开后的 SQL 超过 ClickHouse 默认 256 KiB 查询长度限制")
    engine("MASS", "需要两层 EMA 后再滚动求和")
    engine("Rank1M", "需要对 20 日收益做每日横截面排名；现有 rank_value 不能替代 raw_value")
    engine("TRIX10", "需要三重 EMA")
    engine("TRIX5", "需要三重 EMA")
    return translations


def parse_catalog(path: Path) -> list[CatalogFactor]:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r'<a id="factor-(?P<anchor>\d+)"></a>\n'
        r'#### (?P<number>\d+)\. `(?P<name>[^`]+)` — (?P<label>.+?)\n\n'
        r'(?P<body>.*?)(?=\n<a id="factor-|\Z)',
        re.S,
    )
    factors: list[CatalogFactor] = []
    for match in pattern.finditer(text):
        body = match.group("body")
        category = _body_field(body, "聚宽分类")
        processing = _body_field(body, "数据处理")
        logic_match = re.search(r"\n计算逻辑：\n\n> (?P<logic>.*)", body, re.S)
        factors.append(
            CatalogFactor(
                number=int(match.group("number")),
                name=match.group("name"),
                label=match.group("label").strip(),
                category=category,
                processing=processing,
                logic=(logic_match.group("logic").strip() if logic_match else ""),
            )
        )
    if len(factors) != 285:
        raise RuntimeError(f"目录应有 285 个因子，实际解析到 {len(factors)} 个")
    return factors


def _body_field(body: str, label: str) -> str:
    match = re.search(rf"^- {re.escape(label)}：(.+)$", body, re.M)
    return match.group(1).strip() if match else "未说明"


def source_snapshot() -> SourceSnapshot:
    settings = load_settings()
    ch = client()
    source = f"{settings.source_database}.{settings.stock_daily_table}"
    columns = tuple(row[0] for row in ch.query(f"DESCRIBE TABLE {source}").result_rows)
    audited_fields = (
        "open", "high", "low", "close", "volume", "amount", "pre_close",
        "turnover_rate", "pct_chg", "pe", "pb", "high_limited", "low_limited",
    )
    coverage_columns = [name for name in audited_fields if name in columns]
    stock_filter = f"""
        {settings.stock_code_column} GLOBAL IN {{stock_codes:Array(String)}}
    """
    stock_codes = [
        row[0]
        for row in ch.query(
            f"""
            SELECT {settings.stock_code_column}
            FROM {settings.source_database}.{settings.stock_basic_table}
            WHERE {settings.stock_basic_type_column} = {{stock_type_value:String}}
            """,
            parameters={"stock_type_value": settings.stock_basic_stock_type_value},
        ).result_rows
    ]
    summary_row = ch.query(
        f"""
        SELECT count(), min({settings.stock_date_column}), max({settings.stock_date_column}),
               uniqExact({settings.stock_code_column})
        FROM {source}
        WHERE {stock_filter}
        """,
        parameters={"stock_codes": stock_codes},
    ).first_row
    coverage = {}
    for field in coverage_columns:
        coverage[field] = float(
            ch.query(
                f"""
                SELECT round(countIf({field} IS NOT NULL) / count() * 100, 4)
                FROM {source}
                WHERE {stock_filter}
                """,
                parameters={"stock_codes": stock_codes},
            ).first_row[0]
        )
    total_row = ch.query(
        f"""
        SELECT count(), uniqExact({settings.stock_code_column})
        FROM {source}
        """
    ).first_row[0]
    total_instruments = ch.query(
        f"SELECT uniqExact({settings.stock_code_column}) FROM {source}"
    ).first_row[0]
    return SourceSnapshot(
        database=settings.source_database,
        table=settings.stock_daily_table,
        code_column=settings.stock_code_column,
        date_column=settings.stock_date_column,
        columns=columns,
        coverage=coverage,
        rows=int(summary_row[0]),
        date_start=summary_row[1],
        date_end=summary_row[2],
        instruments=int(summary_row[3]),
        total_rows=int(total_row),
        total_instruments=int(total_instruments),
    )


def compile_translation(translation: Translation, snapshot: SourceSnapshot) -> tuple[list[str], int, str | None]:
    if not translation.expression:
        return [], 1, None
    try:
        compiled = compile_qlib_formula(
            translation.expression,
            params={},
            code_column=snapshot.code_column,
            date_column=snapshot.date_column,
        )
    except FormulaError as exc:
        return [], 1, str(exc)
    missing_fields = sorted(set(compiled.fields) - set(snapshot.columns))
    if missing_fields:
        return compiled.fields, compiled.max_window, "缺少字段: " + ", ".join(missing_fields)
    return compiled.fields, compiled.max_window, None


def runtime_probe(
    translations: dict[str, Translation], snapshot: SourceSnapshot
) -> dict[str, str]:
    """Execute one bounded ClickHouse query for every translatable expression."""
    ch = client()
    source = f"{snapshot.database}.{snapshot.table}"
    settings = load_settings()
    probe_code = ch.query(
        f"""
        SELECT {snapshot.code_column}
        FROM {source}
        WHERE {snapshot.code_column} IN (
            SELECT {snapshot.code_column}
            FROM {settings.source_database}.{settings.stock_basic_table}
            WHERE {settings.stock_basic_type_column} = {{stock_type_value:String}}
        )
        ORDER BY {snapshot.code_column}
        LIMIT 1
        """,
        parameters={"stock_type_value": settings.stock_basic_stock_type_value},
    ).first_row[0]
    probe_start = snapshot.date_end - timedelta(days=550)
    failures: dict[str, str] = {}
    for name, translation in translations.items():
        if translation.status not in {READY, REVIEW} or not translation.expression:
            continue
        try:
            compiled = compile_qlib_formula(
                translation.expression,
                params={},
                code_column=snapshot.code_column,
                date_column=snapshot.date_column,
            )
            ch.query(
                f"""
                SELECT raw_value
                FROM (
                    SELECT {compiled.sql} AS raw_value
                    FROM {source}
                    WHERE {snapshot.code_column} = {{probe_code:String}}
                      AND {snapshot.date_column} >= {{probe_start:Date}}
                      AND {snapshot.date_column} <= {{probe_end:Date}}
                )
                LIMIT 1
                """,
                parameters={
                    "probe_code": probe_code,
                    "probe_start": probe_start,
                    "probe_end": snapshot.date_end,
                },
            )
        except Exception as exc:  # ClickHouse errors have no stable common base class.
            failures[name] = str(exc).splitlines()[0]
    return failures


def registered_factors(snapshot: SourceSnapshot) -> list[dict[str, Any]]:
    settings = load_settings()
    ch = client()
    definitions = ch.query(
        f"""
        SELECT factor_id,
               argMax(version, updated_at) AS version,
               argMax(label, updated_at) AS label,
               argMax(expression, updated_at) AS expression,
               argMax(enabled, updated_at) AS enabled
        FROM {settings.clickhouse_database}.factor_definitions
        GROUP BY factor_id
        ORDER BY factor_id
        """
    ).result_rows
    values = {
        (row[0], int(row[1])): row[2:]
        for row in ch.query(
            f"""
            SELECT factor_id, factor_version, count(), min(trade_date), max(trade_date), uniqExact(entity_code)
            FROM {settings.clickhouse_database}.factor_values_daily
            GROUP BY factor_id, factor_version
            """
        ).result_rows
    }
    rows: list[dict[str, Any]] = []
    for factor_id, version, label, expression, enabled in definitions:
        try:
            compiled = compile_qlib_formula(
                expression,
                params={"window": 20, "vol_window": 20, "return_window": 5, "volume_window": 20,
                        "rv_weight": 0.35, "downside_weight": 0.3, "loss_weight": 0.2,
                        "volume_weight": 0.15, "volume_scale": 10},
                code_column=snapshot.code_column,
                date_column=snapshot.date_column,
            )
            missing_fields = sorted(set(compiled.fields) - set(snapshot.columns))
            error = "缺少字段: " + ", ".join(missing_fields) if missing_fields else None
            fields = compiled.fields
        except FormulaError as exc:
            error = str(exc)
            fields = []
        persisted = values.get((factor_id, int(version)), (0, None, None, 0))
        rows.append(
            {
                "factor_id": factor_id,
                "version": int(version),
                "label": label,
                "enabled": bool(enabled),
                "fields": fields,
                "error": error,
                "rows": int(persisted[0]),
                "date_start": persisted[1],
                "date_end": persisted[2],
                "instruments": int(persisted[3]),
            }
        )
    return rows


def missing_data_reason(factor: CatalogFactor) -> str:
    category_reasons = {
        "基础科目及衍生类因子": "缺财务报表、现金流、市值等源字段",
        "质量类因子": "缺资产负债表、利润表、现金流量表等财务字段",
        "每股指标因子": "缺财务科目和总股本字段",
        "成长类因子": "缺多期财务与同比增长字段",
        "风险因子 - 新风格因子": "缺组成因子、行业/市值暴露或公开公式",
    }
    if factor.category == "风险因子 - 风格因子":
        return "缺市场指数、行业、市值、分析师预测或复合因子依赖"
    return category_reasons.get(factor.category, "当前源字段不足")


def render_report(
    factors: list[CatalogFactor],
    snapshot: SourceSnapshot,
    translations: dict[str, Translation],
    registered: list[dict[str, Any]],
    runtime_errors: dict[str, str],
    runtime_probed: bool,
) -> str:
    rows: list[dict[str, Any]] = []
    for factor in factors:
        translation = translations.get(factor.name, Translation(MISSING, None, missing_data_reason(factor)))
        fields, max_window, error = compile_translation(translation, snapshot)
        if not error:
            error = runtime_errors.get(factor.name)
        status = translation.status
        note = translation.note
        if error:
            status = ENGINE if translation.status in {READY, REVIEW} else translation.status
            note = f"{note}；检测失败：{error}"
        if status == READY:
            minimum_coverage = min((snapshot.coverage.get(field, 100.0) for field in fields), default=100.0)
            status = "ready_full" if minimum_coverage >= 99.999 else "ready_partial"
        rows.append(
            {
                "factor": factor,
                "translation": translation,
                "status": status,
                "fields": fields,
                "max_window": max_window,
                "note": note,
            }
        )

    counts = Counter(row["status"] for row in rows)
    exact_processing = sum(
        row["status"] in {"ready_full", "ready_partial"}
        and row["factor"].processing == "无"
        for row in rows
    )
    ready_count = counts["ready_full"] + counts["ready_partial"]
    runtime_probe_count = sum(
        item.status in {READY, REVIEW} and bool(item.expression)
        for item in translations.values()
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["factor"].category].append(row)

    lines = [
        "# 聚宽因子与当前系统同步能力检测",
        "",
        f"> 检测时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}。",
        "> “可同步”表示当前 ClickHouse 日频源字段存在、表达式能通过现有编译器，",
        "> 不代表数值已与聚宽逐日对齐，也不代表聚宽的行业/市值中性化已经复刻。",
        "",
        "## 结论",
        "",
        f"- 聚宽目录：{len(rows)} 个。",
        f"- **现在可同步 raw_value：{ready_count} 个**，其中全历史核心行情字段 {counts['ready_full']} 个，依赖部分历史字段 {counts['ready_partial']} 个。",
        f"- 公式口径需人工确认：{counts[REVIEW]} 个。",
        f"- 数据够、但计算引擎需扩展：{counts[ENGINE]} 个。",
        f"- 当前缺数据或复合依赖：{counts[MISSING]} 个。",
        (
            f"- 真实 ClickHouse 执行探测：{runtime_probe_count - len(runtime_errors)}/{runtime_probe_count} 个候选表达式通过。"
            if runtime_probed
            else "- 真实 ClickHouse 执行探测：本次跳过。"
        ),
        f"- 在可同步因子中，聚宽标记为“无数据处理”的有 {exact_processing} 个；其余大多要求行业/市值中性化，当前只能同步原始值，不能声称完全复刻聚宽 score。",
        "",
        "## 当前源实测",
        "",
        f"- 表：`{snapshot.database}.{snapshot.table}`",
        f"- 行数：{snapshot.rows:,}",
        f"- 日期：{snapshot.date_start} 至 {snapshot.date_end}",
        f"- worker 实际股票行数：{snapshot.rows:,}",
        f"- worker 实际股票代码数：{snapshot.instruments:,}",
        f"- 底层视图未过滤规模：{snapshot.total_rows:,} 行、{snapshot.total_instruments:,} 个代码",
        "",
        "| 字段 | 非空覆盖率 |",
        "|---|---:|",
    ]
    important_fields = [
        "open", "high", "low", "close", "volume", "amount", "pre_close",
        "turnover_rate", "pct_chg", "pe", "pb", "high_limited", "low_limited",
    ]
    for field in important_fields:
        if field in snapshot.coverage:
            lines.append(f"| `{field}` | {snapshot.coverage[field]:.2f}% |")

    lines.extend(["", f"## 现在可同步的 {ready_count} 个聚宽因子", ""])
    for category, category_rows in grouped.items():
        full = [row["factor"].name for row in category_rows if row["status"] == "ready_full"]
        partial = [row["factor"].name for row in category_rows if row["status"] == "ready_partial"]
        if not full and not partial:
            continue
        lines.append(f"### {category}")
        lines.append("")
        if full:
            lines.append("- 完整历史：" + "、".join(f"`{name}`" for name in full))
        if partial:
            lines.append("- 部分历史：" + "、".join(f"`{name}`" for name in partial))
        lines.append("")

    lines.extend(
        [
            "",
            "## 系统已登记因子",
            "",
            "| 因子 | 最新版本 | 同步条件 | 最新版本持久化 | 字段 |",
            "|---|---:|---|---|---|",
        ]
    )
    for item in registered:
        capability = "可同步" if item["enabled"] and not item["error"] else (item["error"] or "已停用")
        persisted = (
            f"{item['rows']:,} 行，{item['date_start']} 至 {item['date_end']}"
            if item["rows"]
            else "0 行（需重算最新版本）"
        )
        lines.append(
            f"| `{item['factor_id']}`（{item['label']}） | v{item['version']} | {capability} | "
            f"{persisted} | {', '.join(f'`{field}`' for field in item['fields']) or '-'} |"
        )

    lines.extend(
        [
            "",
            "## 285 个聚宽因子的逐项结果",
            "",
        "状态说明：`可同步-完整` 的依赖字段在 worker 股票范围内 100% 非空；`可同步-部分` 至少有一个依赖字段存在历史缺失。",
            "",
        ]
    )
    labels = {
        "ready_full": "可同步-完整",
        "ready_partial": "可同步-部分",
        REVIEW: "口径待确认",
        ENGINE: "需扩展引擎",
        MISSING: "缺数据/依赖",
    }
    for category, category_rows in grouped.items():
        lines.extend(
            [
                f"### {category}",
                "",
                "| 因子 | 状态 | 依赖字段 | 目标表达式/阻塞原因 | 聚宽处理 |",
                "|---|---|---|---|---|",
            ]
        )
        for row in category_rows:
            factor = row["factor"]
            translation = row["translation"]
            fields = ", ".join(f"`{field}`" for field in row["fields"]) or "-"
            detail = translation.expression or row["note"]
            if translation.expression and row["status"] in {REVIEW, ENGINE}:
                detail = f"`{translation.expression}`；{row['note']}"
            elif translation.expression:
                detail = f"`{translation.expression}`"
            detail = detail.replace("|", r"\|")
            processing = factor.processing.replace("|", r"\|")
            lines.append(
                f"| `{factor.name}` | {labels[row['status']]} | {fields} | {detail} | {processing} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 检测口径与下一步",
            "",
            "1. 本报告验证的是表达式可编译、字段真实存在以及字段非空覆盖率；没有批量创建 85 个因子，也没有启动大规模同步任务。",
            "2. 可同步因子的 `raw_value` 可以先落库；若沿用系统默认处理，`score` 是当前系统的横截面缩尾 Z-score，不等于聚宽含行业/市值中性化的 score。",
            "3. 建议先从 100% 行情覆盖组挑 5 至 10 个代表因子做逐日抽样对齐，再发布全量定义。",
            "4. 重新检测：`rtk .venv/bin/python scripts/audit_joinquant_factor_compatibility.py`。",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default="docs/joinquant-factor-catalog.md")
    parser.add_argument("--output", default="docs/joinquant-factor-compatibility.md")
    parser.add_argument(
        "--skip-runtime-probe",
        action="store_true",
        help="Skip bounded ClickHouse execution probes and only compile expressions.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    factors = parse_catalog(Path(args.catalog))
    snapshot = source_snapshot()
    translations = build_translations()
    catalog_names = {factor.name for factor in factors}
    unknown = sorted(set(translations) - catalog_names)
    if unknown:
        raise RuntimeError("翻译表含目录之外的因子: " + ", ".join(unknown))
    runtime_errors = {} if args.skip_runtime_probe else runtime_probe(translations, snapshot)
    report = render_report(
        factors,
        snapshot,
        translations,
        registered_factors(snapshot),
        runtime_errors,
        not args.skip_runtime_probe,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(f"已生成 {output}: {len(factors)} 个因子，已翻译/分类 {len(translations)} 个")


if __name__ == "__main__":
    main()
