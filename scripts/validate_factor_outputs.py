from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factor_service import repository
from factor_service.clickhouse import client, settings
from factor_service.worker import _build_value_sql
from factor_service.qlib_formula import compile_qlib_formula


@dataclass
class Check:
    status: str
    name: str
    detail: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate factor formulas and stored outputs.")
    parser.add_argument("--date-start", default="2026-04-01")
    parser.add_argument("--date-end", default="2026-04-30")
    parser.add_argument("--market-db", default="starlight")
    args = parser.parse_args()

    checks: list[Check] = []
    config = settings()

    checks.append(check_configured_source(config))
    checks.extend(check_starlight_sources(args.market_db))
    checks.extend(check_factor_definitions())
    checks.extend(check_synthetic_formula_functions())
    checks.extend(check_real_formula_cross_checks(args.date_start, args.date_end))
    checks.append(check_latest_date_empty_result())
    checks.append(check_first_limit_up_worker_preview(args.date_start, args.date_end))
    checks.append(check_first_limit_up(args.market_db, args.date_start, args.date_end))
    checks.append(check_period_return(args.market_db, args.date_start, args.date_end))

    for check in checks:
        print(f"[{check.status}] {check.name}: {check.detail}")

    failed = [check for check in checks if check.status == "FAIL"]
    print(f"\nsummary: {len(failed)} failed / {len(checks)} checks")
    return 1 if failed else 0


def check_configured_source(config: Any) -> Check:
    source = f"{config.source_database}.{config.stock_daily_table}"
    try:
        rows = client().query(
            f"""
            SELECT count(), min({config.stock_date_column}), max({config.stock_date_column})
            FROM {source}
            """
        ).result_rows
    except Exception as exc:
        return Check("FAIL", "configured source", f"{source} query failed: {exc}")

    count, date_start, date_end = rows[0] if rows else (0, None, None)
    if int(count or 0) == 0:
        return Check("FAIL", "configured source", f"{source} is empty")
    return Check("OK", "configured source", f"{source} rows={count} range={date_start}..{date_end}")


def check_starlight_sources(database: str) -> list[Check]:
    specs = [
        ("market kline", f"{database}.ad_market_kline_daily", "toDate(trade_time)", "code"),
        ("stock status", f"{database}.ad_history_stock_status", "trade_date", "market_code"),
    ]
    checks: list[Check] = []
    for name, table, date_expr, code_column in specs:
        try:
            rows = client().query(
                f"""
                SELECT count(), uniqExact({code_column}), min({date_expr}), max({date_expr})
                FROM {table}
                """
            ).result_rows
        except Exception as exc:
            checks.append(Check("FAIL", name, f"{table} query failed: {exc}"))
            continue
        count, codes, date_start, date_end = rows[0] if rows else (0, 0, None, None)
        status = "OK" if int(count or 0) > 0 else "FAIL"
        checks.append(Check(status, name, f"{table} rows={count} codes={codes} range={date_start}..{date_end}"))
    return checks


def check_factor_definitions() -> list[Check]:
    checks: list[Check] = []
    config = settings()
    source_columns = table_columns(config.source_database, config.stock_daily_table)
    factors = repository.list_factors(entity_type="stock")
    for factor in factors:
        name = f"formula {factor.factor_id} v{factor.version}"
        try:
            compiled = compile_qlib_formula(
                factor.expression,
                params=factor.params,
                code_column=config.stock_code_column,
                date_column=config.stock_date_column,
            )
        except Exception as exc:
            checks.append(Check("FAIL", name, f"compile failed: {exc}"))
            continue

        missing = sorted(set(compiled.fields) - source_columns)
        if missing:
            checks.append(Check("FAIL", name, f"compiled fields={compiled.fields}; missing in configured source={missing}"))
        else:
            checks.append(Check("OK", name, f"compiled fields={compiled.fields}; max_window={compiled.max_window}"))

    limit_up = repository.get_factor("limit_up_count")
    if limit_up and "close" not in limit_up.expression:
        checks.append(Check("FAIL", "semantic limit_up_count", "expression sums high_limited directly; should count close >= high_limited"))
    return checks


def check_latest_date_empty_result() -> Check:
    value = repository.latest_value_date("first_limit_up_window", factor_version=999999)
    if value is None:
        return Check("OK", "empty latest date", "missing result returns None")
    return Check("FAIL", "empty latest date", f"missing result returned {value}")


def check_synthetic_formula_functions() -> list[Check]:
    exact_cases = [
        ("Add", "Add($close, $open)", "2020-01-05", 35.0),
        ("Sub", "Sub($close, $open)", "2020-01-05", 1.0),
        ("Mul", "Mul($close, $open)", "2020-01-05", 306.0),
        ("Div", "Div($close, $open)", "2020-01-05", 18 / 17),
        ("Greater", "Greater($close, $open)", "2020-01-05", 18.0),
        ("Less", "Less($close, $open)", "2020-01-05", 17.0),
        ("Abs", "Abs(Sub($open, $close))", "2020-01-05", 1.0),
        ("Sign", "Sign(Sub($open, $close))", "2020-01-05", -1.0),
        ("Log", "Log($close)", "2020-01-05", math.log(18)),
        ("Power", "Power($close, 2)", "2020-01-05", 324.0),
        ("NullIf", "NullIf($close, $close)", "2020-01-05", None),
        ("IsNull", "IsNull(NullIf($close, $close))", "2020-01-05", 1.0),
        ("Fillna", "Fillna(NullIf($close, $close), 5)", "2020-01-05", 5.0),
        ("If", "If(Gt($close, $open), 1, 0)", "2020-01-05", 1.0),
        ("And", "And(Gt($close, $open), Lt($close, $high))", "2020-01-05", 1.0),
        ("Or", "Or(Lt($close, $open), Lt($close, $high))", "2020-01-05", 1.0),
        ("Not", "Not(Lt($close, $open))", "2020-01-05", 1.0),
        ("Gt", "Gt($close, $open)", "2020-01-05", 1.0),
        ("Ge", "Ge($close, $close)", "2020-01-05", 1.0),
        ("Lt", "Lt($open, $close)", "2020-01-05", 1.0),
        ("Le", "Le($close, $close)", "2020-01-05", 1.0),
        ("Eq", "Eq($close, $close)", "2020-01-05", 1.0),
        ("Ne", "Ne($close, $open)", "2020-01-05", 1.0),
        ("Mean", "Mean($close, 3)", "2020-01-05", 44 / 3),
        ("Sum", "Sum($volume, 3)", "2020-01-05", 1200.0),
        ("Max", "Max($high, 3)", "2020-01-05", 19.0),
        ("Min", "Min($low, 3)", "2020-01-05", 10.0),
        ("Med", "Med($close, 3)", "2020-01-05", 15.0),
        ("Count", "Count($close, 3)", "2020-01-05", 3.0),
        ("Ref", "Ref($close, 1)", "2020-01-05", 15.0),
        ("Delta", "Delta($close, 1)", "2020-01-05", 3.0),
        ("PeriodReturn", "PeriodReturn($close, 1)", "2020-01-05", 0.2),
        ("FirstTrue true", "FirstTrue(And(Gt($high_limited, 0), Ge($close, $high_limited)), 3)", "2020-01-02", 1.0),
        ("FirstTrue repeated", "FirstTrue(And(Gt($high_limited, 0), Ge($close, $high_limited)), 3)", "2020-01-05", 0.0),
        ("EMA", "EMA($close, 3)", "2020-01-05", (18 * 0.5 + 15 * 0.25 + 11 * 0.125) / (0.5 + 0.25 + 0.125)),
        ("WMA", "WMA($close, 3)", "2020-01-05", 95 / 6),
        ("Mad", "Mad($close, 3)", "2020-01-05", (abs(18 - 44 / 3) + abs(15 - 44 / 3) + abs(11 - 44 / 3)) / 3),
        ("Rank", "Rank($close, 3)", "2020-01-05", 1.0),
    ]
    smoke_cases = [
        ("Std", "Std($close, 3)"),
        ("Var", "Var($close, 3)"),
        ("Skew", "Skew($close, 3)"),
        ("Kurt", "Kurt($close, 3)"),
        ("Quantile", "Quantile($close, 3, 0.5)"),
        ("IdxMax", "IdxMax($high, 3)"),
        ("IdxMin", "IdxMin($low, 3)"),
        ("Corr", "Corr($close, $volume, 3)"),
        ("Cov", "Cov($close, $volume, 3)"),
        ("Slope", "Slope($close, 3)"),
        ("Rsquare", "Rsquare($close, 3)"),
        ("Resi", "Resi($close, 3)"),
    ]

    failures: list[str] = []
    for name, expression, target_date, expected in exact_cases:
        try:
            actual = synthetic_formula_value(expression, target_date)
        except Exception as exc:
            failures.append(f"{name}: execution failed: {exc}")
            continue
        if not values_close(actual, expected):
            failures.append(f"{name}: expected={expected!r}, actual={actual!r}")

    for name, expression in smoke_cases:
        try:
            actual = synthetic_formula_value(expression, "2020-01-05")
        except Exception as exc:
            failures.append(f"{name}: execution failed: {exc}")
            continue
        if actual is None:
            failures.append(f"{name}: returned NULL on complete synthetic window")

    if failures:
        return [Check("FAIL", "synthetic formula functions", "; ".join(failures[:6]))]
    return [Check("OK", "synthetic formula functions", f"{len(exact_cases)} exact cases + {len(smoke_cases)} smoke cases passed")]


def synthetic_formula_value(expression: str, target_date: str) -> Any:
    compiled = compile_qlib_formula(expression, params={}, code_column="code", date_column="trade_time")
    rows = client().query(
        f"""
        SELECT raw_value
        FROM (
            SELECT
                trade_time,
                code,
                {compiled.sql} AS raw_value
            FROM {synthetic_source_sql()}
        )
        WHERE code = 'AAA'
          AND trade_time = {{target_date:Date}}
        """,
        parameters={"target_date": date.fromisoformat(target_date)},
    ).result_rows
    return rows[0][0] if rows else None


def synthetic_source_sql() -> str:
    return """
    values(
        'code String, trade_time Date, close Nullable(Float64), open Nullable(Float64), high Nullable(Float64), low Nullable(Float64), volume Nullable(Float64), high_limited Nullable(Float64)',
        ('AAA', '2020-01-01', 10.0, 9.0, 11.0, 8.0, 100.0, 0.0),
        ('AAA', '2020-01-02', 12.0, 11.0, 13.0, 10.0, 200.0, 12.0),
        ('AAA', '2020-01-03', 11.0, 12.0, 12.0, 10.0, 300.0, 0.0),
        ('AAA', '2020-01-04', 15.0, 14.0, 16.0, 13.0, 400.0, 15.0),
        ('AAA', '2020-01-05', 18.0, 17.0, 19.0, 16.0, 500.0, 18.0)
    )
    """


def values_close(actual: Any, expected: Any, tolerance: float = 1e-9) -> bool:
    if expected is None:
        return actual is None
    if actual is None:
        return False
    return abs(float(actual) - float(expected)) <= tolerance


def check_real_formula_cross_checks(date_start: str, date_end: str) -> list[Check]:
    specs = [
        ("mean_volume", lambda window, code_column, date_column: rolling_sql("avg", "volume", window, code_column, date_column)),
        ("mean_amount", lambda window, code_column, date_column: rolling_sql("avg", "amount", window, code_column, date_column)),
        ("mean_turnover_rate", lambda window, code_column, date_column: rolling_sql("avg", "turnover_rate", window, code_column, date_column)),
        ("limit_up_count", limit_up_count_sql),
        ("first_limit_up_window", first_limit_up_sql),
        ("period_return", period_return_sql),
    ]
    checks: list[Check] = []
    for factor_id, manual_builder in specs:
        checks.append(check_real_formula_against_manual(factor_id, manual_builder, date_start, date_end))
    return checks


def check_real_formula_against_manual(factor_id: str, manual_builder, date_start: str, date_end: str) -> Check:
    config = settings()
    factor = repository.get_factor(factor_id)
    if not factor:
        return Check("FAIL", f"real formula {factor_id}", "factor not found")

    source = f"{config.source_database}.{config.stock_daily_table}"
    universe_filter = f"""
        AND {config.stock_code_column} IN (
            SELECT {config.stock_code_column}
            FROM {config.source_database}.{config.stock_basic_table}
            WHERE {config.stock_basic_type_column} = {{stock_type_value:String}}
        )
    """
    value_plan = _build_value_sql(
        factor.expression,
        source=source,
        code_column=config.stock_code_column,
        date_column=config.stock_date_column,
        params=factor.params,
        universe_filter=universe_filter,
    )
    window = int(factor.params.get("window", 20))
    manual_sql = manual_builder(window, config.stock_code_column, config.stock_date_column)
    start = date.fromisoformat(date_start)
    end = date.fromisoformat(date_end)
    source_start = start - timedelta(days=max(value_plan.max_window * 4 + 20, 90))

    comparison_sql = f"""
    WITH compiled AS (
        SELECT trade_date, entity_code, raw_value AS compiled
        FROM ({value_plan.sql})
        WHERE trade_date >= {{date_start:Date}}
          AND trade_date <= {{date_end:Date}}
          AND raw_value IS NOT NULL
    ), manual_base AS (
        SELECT
            {config.stock_date_column} AS trade_date,
            {config.stock_code_column} AS entity_code,
            {manual_sql} AS expected
        FROM {source}
        WHERE {config.stock_date_column} >= {{source_start:Date}}
          AND {config.stock_date_column} <= {{date_end:Date}}
          {universe_filter}
    ), manual AS (
        SELECT trade_date, entity_code, expected
        FROM manual_base
        WHERE trade_date >= {{date_start:Date}}
          AND trade_date <= {{date_end:Date}}
          AND expected IS NOT NULL
    )
    SELECT
        (SELECT count() FROM manual) AS expected_rows,
        (SELECT count() FROM compiled) AS compiled_rows,
        count() AS joined_rows,
        countIf(abs(expected - compiled) > 1e-9) AS mismatches,
        max(abs(expected - compiled)) AS max_diff,
        min(compiled) AS min_value,
        max(compiled) AS max_value
    FROM manual
    INNER JOIN compiled USING (trade_date, entity_code)
    """
    try:
        expected_rows, compiled_rows, joined_rows, mismatches, max_diff, min_value, max_value = client().query(
            comparison_sql,
            parameters={
                "source_start": source_start,
                "date_start": start,
                "date_end": end,
                "stock_type_value": config.stock_basic_stock_type_value,
            },
        ).result_rows[0]
    except Exception as exc:
        return Check("FAIL", f"real formula {factor_id}", f"comparison query failed: {exc}")

    detail = (
        f"v{factor.version} expected={expected_rows} compiled={compiled_rows} "
        f"joined={joined_rows} mismatches={mismatches} max_diff={max_diff} "
        f"range={min_value}..{max_value}"
    )
    if int(expected_rows or 0) == 0 or int(compiled_rows or 0) == 0:
        return Check("FAIL", f"real formula {factor_id}", detail)
    if int(expected_rows or 0) != int(compiled_rows or 0) or int(joined_rows or 0) != int(expected_rows or 0):
        return Check("FAIL", f"real formula {factor_id}", detail)
    if int(mismatches or 0) > 0:
        return Check("FAIL", f"real formula {factor_id}", detail)
    return Check("OK", f"real formula {factor_id}", detail)


def rolling_sql(function: str, field: str, window: int, code_column: str, date_column: str) -> str:
    return f"{function}({field}) {manual_window_clause(window, code_column, date_column)}"


def limit_up_count_sql(window: int, code_column: str, date_column: str) -> str:
    signal = "if(high_limited > 0 AND close >= high_limited, 1, 0)"
    return f"sum({signal}) {manual_window_clause(window, code_column, date_column)}"


def first_limit_up_sql(window: int, code_column: str, date_column: str) -> str:
    signal = "(high_limited > 0 AND close >= high_limited)"
    truth = f"if(isNull({signal}), 0, if({signal} != 0, 1, 0))"
    if window <= 1:
        return f"toFloat64({truth})"
    previous = (
        f"OVER (PARTITION BY {code_column} ORDER BY {date_column} "
        f"ROWS BETWEEN {window - 1} PRECEDING AND 1 PRECEDING)"
    )
    return f"if({truth} = 1 AND coalesce(sum({truth}) {previous}, 0) = 0, 1.0, 0.0)"


def period_return_sql(window: int, code_column: str, date_column: str) -> str:
    lag_expr = f"lagInFrame(close, {window}) {manual_window_clause(window + 1, code_column, date_column, preceding=window)}"
    return f"if(isNull({lag_expr}) OR {lag_expr} = 0 OR isNull(close), NULL, close / {lag_expr} - 1)"


def manual_window_clause(window: int, code_column: str, date_column: str, *, preceding: int | None = None) -> str:
    preceding_rows = window - 1 if preceding is None else preceding
    return (
        f"OVER (PARTITION BY {code_column} ORDER BY {date_column} "
        f"ROWS BETWEEN {preceding_rows} PRECEDING AND CURRENT ROW)"
    )


def check_first_limit_up(_database: str, date_start: str, date_end: str) -> Check:
    expected_rows, expected_positive, _, _ = first_limit_up_worker_counts(date_start, date_end)
    actual_sql = """
    SELECT count(), countIf(raw_value = 1)
    FROM (
        SELECT trade_date, entity_code, max(raw_value) AS raw_value
        FROM ab_factor.factor_values_daily
        WHERE factor_id = 'first_limit_up_window'
          AND factor_version = 2
          AND trade_date >= {date_start:Date}
          AND trade_date <= {date_end:Date}
        GROUP BY trade_date, entity_code
    )
    """
    actual_rows, actual_positive = client().query(
        actual_sql,
        parameters={"date_start": date.fromisoformat(date_start), "date_end": date.fromisoformat(date_end)},
    ).result_rows[0]

    detail = (
        f"expected rows={expected_rows} positives={expected_positive}; "
        f"actual rows={actual_rows} positives={actual_positive}"
    )
    if int(expected_positive or 0) > 0 and int(actual_rows or 0) == 0:
        return Check("FAIL", "stored first_limit_up_window v2", detail)
    if int(expected_positive or 0) != int(actual_positive or 0):
        return Check("FAIL", "stored first_limit_up_window v2", detail)
    return Check("OK", "stored first_limit_up_window v2", detail)


def check_first_limit_up_worker_preview(date_start: str, date_end: str) -> Check:
    row_count, positive_count, min_date, max_date = first_limit_up_worker_counts(date_start, date_end)
    detail = f"rows={row_count} positives={positive_count} range={min_date}..{max_date}"
    if int(row_count or 0) == 0:
        return Check("FAIL", "worker preview first_limit_up_window v2", detail)
    if int(positive_count or 0) == 0:
        return Check("FAIL", "worker preview first_limit_up_window v2", detail)
    return Check("OK", "worker preview first_limit_up_window v2", detail)


def first_limit_up_worker_counts(date_start: str, date_end: str) -> tuple[int, int, Any, Any]:
    config = settings()
    factor = repository.get_factor("first_limit_up_window")
    if not factor:
        return 0, 0, None, None

    value_plan = _build_value_sql(
        factor.expression,
        source=f"{config.source_database}.{config.stock_daily_table}",
        code_column=config.stock_code_column,
        date_column=config.stock_date_column,
        params=factor.params,
        universe_filter=f"""
            AND {config.stock_code_column} IN (
                SELECT {config.stock_code_column}
                FROM {config.source_database}.{config.stock_basic_table}
                WHERE {config.stock_basic_type_column} = {{stock_type_value:String}}
            )
        """,
    )
    start = date.fromisoformat(date_start)
    end = date.fromisoformat(date_end)
    source_start = start.toordinal() - max(value_plan.max_window * 4 + 20, 90)
    source_start_date = date.fromordinal(source_start)
    rows = client().query(
        f"""
        SELECT count(), countIf(raw_value = 1), min(trade_date), max(trade_date)
        FROM ({value_plan.sql})
        WHERE trade_date >= {{date_start:Date}}
          AND trade_date <= {{date_end:Date}}
          AND raw_value IS NOT NULL
        """,
        parameters={
            "source_start": source_start_date,
            "date_start": start,
            "date_end": end,
            "stock_type_value": config.stock_basic_stock_type_value,
        },
    ).result_rows
    return rows[0] if rows else (0, 0, None, None)


def check_period_return(_database: str, date_start: str, date_end: str) -> Check:
    config = settings()
    factor = repository.get_factor("period_return")
    if not factor:
        return Check("FAIL", "period_return", "factor not found")

    value_plan = _build_value_sql(
        factor.expression,
        source=f"{config.source_database}.{config.stock_daily_table}",
        code_column=config.stock_code_column,
        date_column=config.stock_date_column,
        params=factor.params,
        universe_filter=f"""
            AND {config.stock_code_column} IN (
                SELECT {config.stock_code_column}
                FROM {config.source_database}.{config.stock_basic_table}
                WHERE {config.stock_basic_type_column} = {{stock_type_value:String}}
            )
        """,
    )
    start = date.fromisoformat(date_start)
    end = date.fromisoformat(date_end)
    source_start = date.fromordinal(start.toordinal() - max(value_plan.max_window * 4 + 20, 90))
    sql = f"""
    WITH expected AS (
        SELECT trade_date, entity_code, raw_value AS expected
        FROM ({value_plan.sql})
        WHERE trade_date >= {{date_start:Date}}
          AND trade_date <= {{date_end:Date}}
          AND raw_value IS NOT NULL
    ), actual AS (
        SELECT trade_date, entity_code, any(raw_value) AS actual
        FROM ab_factor.factor_values_daily
        WHERE factor_id = 'period_return'
          AND factor_version = {{factor_version:UInt32}}
          AND trade_date >= {{date_start:Date}}
          AND trade_date <= {{date_end:Date}}
        GROUP BY trade_date, entity_code
    )
    SELECT
        (SELECT count() FROM expected) AS expected_rows,
        (SELECT count() FROM actual) AS actual_rows,
        count() AS joined_rows,
        countIf(abs(expected - actual) > 1e-9) AS mismatches,
        max(abs(expected - actual)) AS max_diff
    FROM expected
    INNER JOIN actual USING (trade_date, entity_code)
    """
    expected_rows, actual_rows, joined_rows, mismatches, max_diff = client().query(
        sql,
        parameters={
            "source_start": source_start,
            "date_start": start,
            "date_end": end,
            "stock_type_value": config.stock_basic_stock_type_value,
            "factor_version": factor.version,
        },
    ).result_rows[0]
    detail = (
        f"v{factor.version} expected={expected_rows} actual={actual_rows} "
        f"joined={joined_rows} mismatches={mismatches} max_diff={max_diff}"
    )
    if int(actual_rows or 0) == 0:
        return Check("FAIL", f"period_return v{factor.version}", detail)
    if int(expected_rows or 0) != int(actual_rows or 0):
        return Check("FAIL", f"period_return v{factor.version}", detail)
    if int(mismatches or 0) > 0:
        return Check("FAIL", f"period_return v{factor.version}", detail)
    return Check("OK", f"period_return v{factor.version}", detail)


def table_columns(database: str, table: str) -> set[str]:
    try:
        rows = client().query(f"DESCRIBE TABLE {database}.{table}").result_rows
    except Exception:
        return set()
    return {row[0] for row in rows}


if __name__ == "__main__":
    sys.exit(main())
