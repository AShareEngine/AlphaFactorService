from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factor_service.clickhouse import client, settings  # noqa: E402
from factor_service.qlib_formula import compile_qlib_formula  # noqa: E402


PRICE_FIELD_RE = re.compile(r"\$(?:asset\.)?(open|high|low|close)\b")
PRICE_FIELD_MAP = {
    "open": "open_adj",
    "high": "high_adj",
    "low": "low_adj",
    "close": "close_adj",
}
REFERENCE_FIELD_MAP = {
    "pre_close": "pre_close_adj",
    "preclose": "pre_close_adj",
    "high_limited": "high_limited_adj",
    "low_limited": "low_limited_adj",
}


def rewrite_expression(expression: str) -> str:
    """Move OHLC formulas to adjusted prices without mixing price scales."""

    def replace_price(match: re.Match[str]) -> str:
        prefix = "$asset." if match.group(0).startswith("$asset.") else "$"
        return f"{prefix}{PRICE_FIELD_MAP[match.group(1)]}"

    rewritten = PRICE_FIELD_RE.sub(replace_price, expression)
    if rewritten == expression:
        return expression
    for source, target in REFERENCE_FIELD_MAP.items():
        rewritten = re.sub(rf"\${source}\b", f"${target}", rewritten)
        rewritten = re.sub(
            rf"\$asset\.{source}\b",
            f"$asset.{target}",
            rewritten,
        )
    return rewritten


def _latest_factor_rows() -> list[tuple[Any, ...]]:
    database = settings().clickhouse_database
    return client().query(
        f"""
        SELECT *
        FROM {database}.factor_definitions
        ORDER BY factor_id ASC, version DESC
        LIMIT 1 BY factor_id
        """
    ).result_rows


def _json_dict(raw: str) -> dict[str, Any]:
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("factor JSON metadata must be an object")
    return value


def _source_columns() -> set[str]:
    config = settings()
    rows = client().query(
        """
        SELECT name
        FROM system.columns
        WHERE database = {database:String} AND table = {table:String}
        """,
        parameters={
            "database": config.source_database,
            "table": config.stock_daily_table,
        },
    ).result_rows
    return {str(row[0]) for row in rows}


def build_migration_rows() -> tuple[list[list[Any]], list[tuple[str, int, int, str, str]]]:
    inserts: list[list[Any]] = []
    changes: list[tuple[str, int, int, str, str]] = []
    required_source_fields: set[str] = set()
    now = datetime.now()

    for row in _latest_factor_rows():
        old_expression = str(row[15])
        new_expression = rewrite_expression(old_expression)
        if new_expression == old_expression:
            continue
        params = _json_dict(str(row[12]))
        compiled = compile_qlib_formula(
            new_expression,
            params=params,
            code_column="code",
            date_column="trade_time",
        )
        required_source_fields.update(compiled.fields)
        next_version = int(row[1]) + 1
        inserts.append(
            [
                row[0],
                next_version,
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                row[7],
                row[8],
                row[9],
                row[10],
                compiled.fields,
                row[12],
                row[13],
                row[14],
                new_expression,
                row[16],
                row[17],
                now,
            ]
        )
        changes.append(
            (str(row[0]), int(row[1]), next_version, old_expression, new_expression)
        )

    missing = sorted(required_source_fields - _source_columns())
    if missing:
        raise ValueError(f"factor source is missing fields: {', '.join(missing)}")
    return inserts, changes


def apply_migration(rows: list[list[Any]]) -> None:
    if not rows:
        return
    database = settings().clickhouse_database
    client().insert(
        f"{database}.factor_definitions",
        rows,
        column_names=[
            "factor_id",
            "version",
            "label",
            "description",
            "entity_type",
            "category",
            "group_name",
            "output_type",
            "frequency",
            "asset_id",
            "source_node_id",
            "required_fields",
            "params_json",
            "param_schema_json",
            "availability_policy_json",
            "expression",
            "enabled",
            "created_at",
            "updated_at",
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Version factor formulas from raw OHLC to adjusted OHLC fields."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Insert the migrated definitions. Without this flag, only preview changes.",
    )
    args = parser.parse_args()

    rows, changes = build_migration_rows()
    for factor_id, old_version, new_version, old_expression, new_expression in changes:
        print(f"{factor_id}: v{old_version} -> v{new_version}")
        print(f"  - {old_expression}")
        print(f"  + {new_expression}")
    print(f"affected factors: {len(changes)}")
    if args.apply:
        apply_migration(rows)
        print(f"inserted factor definitions: {len(rows)}")
    else:
        print("dry run only; pass --apply to insert new versions")


if __name__ == "__main__":
    main()
