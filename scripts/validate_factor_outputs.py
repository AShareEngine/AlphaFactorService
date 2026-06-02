from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
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
