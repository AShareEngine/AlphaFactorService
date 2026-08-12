#!/usr/bin/env python3
"""Import audited JoinQuant factors into the ClickHouse-backed factor library."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from factor_service import repository
from factor_service.qlib_formula import compile_qlib_formula
from factor_service.schemas import FactorCreate
from scripts.audit_joinquant_factor_compatibility import READY, build_translations, parse_catalog


CATEGORY_GROUPS = {
    "风险因子 - 风格因子": "joinquant_style",
    "情绪类因子": "joinquant_sentiment",
    "风险类因子": "joinquant_risk",
    "技术指标因子": "joinquant_technical",
    "动量类因子": "joinquant_momentum",
    "风险因子 - 新风格因子": "joinquant_new_style",
}


def build_factor_payloads(catalog_path: Path) -> list[FactorCreate]:
    catalog = parse_catalog(catalog_path)
    translations = build_translations()
    payloads: list[FactorCreate] = []
    for factor in catalog:
        translation = translations.get(factor.name)
        if translation is None or translation.status != READY or not translation.expression:
            continue
        no_processing = factor.processing == "无"
        processing = {
            "winsorize": "none" if no_processing else "median",
            "neutralize": [],
            "standardize": "none" if no_processing else "zscore",
        }
        logic = factor.logic.splitlines()[0].strip() if factor.logic else "聚宽公开详情未提供计算说明"
        migration_note = (
            "聚宽标记为无后处理，score 保留公式值。"
            if no_processing
            else "当前执行中位数去极值和横截面 Z-score；聚宽要求的行业/市值中性化待数据源接入后补齐。"
        )
        payloads.append(
            FactorCreate(
                factor_id=factor.name,
                label=factor.label,
                description=f"来源：聚宽公开因子库。计算说明：{logic}。迁移口径：{migration_note}",
                entity_type="stock",
                category=factor.category,
                group_name=CATEGORY_GROUPS[factor.category],
                output_type="number",
                frequency="daily",
                asset_id="stock",
                source_node_id="stock_daily_real",
                params={
                    "_specs": [
                        {
                            "spec_id": f"{factor.name}__default",
                            "label": f"{factor.label}默认规格",
                            "params": {},
                            "is_default": True,
                            "sync_mode": "scheduled",
                            "enabled": True,
                            "created_from": "joinquant_import",
                        }
                    ],
                    "data_processing": processing,
                    "weighting": "equal",
                },
                expression=translation.expression,
                enabled=True,
            )
        )
    if len(payloads) != 84:
        raise RuntimeError(f"审计通过的因子应为 84 个，实际生成 {len(payloads)} 个")
    return payloads


def validate_payloads(payloads: list[FactorCreate]) -> None:
    seen: set[str] = set()
    for payload in payloads:
        if payload.factor_id in seen:
            raise RuntimeError(f"因子标识重复: {payload.factor_id}")
        seen.add(payload.factor_id)
        compiled = compile_qlib_formula(
            payload.expression,
            params=payload.params,
            code_column="code",
            date_column="trade_time",
        )
        if not compiled.fields:
            raise RuntimeError(f"因子没有源字段依赖: {payload.factor_id}")


def import_payloads(
    payloads: list[FactorCreate],
    *,
    update_existing: bool,
) -> tuple[Counter, list[str]]:
    outcomes: Counter = Counter()
    conflicts: list[str] = []
    for payload in payloads:
        try:
            _, outcome = repository.ensure_factor_definition(
                payload,
                update_existing=update_existing,
            )
            outcomes[outcome] += 1
        except ValueError as exc:
            outcomes["conflict"] += 1
            conflicts.append(str(exc))
    return outcomes, conflicts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default="docs/joinquant-factor-catalog.md")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write definitions to ClickHouse. Without this flag the command only validates.",
    )
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="Create a new version when an existing factor ID has a different definition.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payloads = build_factor_payloads(Path(args.catalog))
    validate_payloads(payloads)
    category_counts = Counter(payload.category for payload in payloads)
    print(f"已验证 {len(payloads)} 个聚宽因子定义")
    print("分类：" + "；".join(f"{name}={count}" for name, count in category_counts.items()))
    if not args.apply:
        print("当前为预检模式，未写入 ClickHouse；使用 --apply 正式导入")
        return
    outcomes, conflicts = import_payloads(payloads, update_existing=args.update_existing)
    print(
        "导入结果："
        + "，".join(
            f"{name}={outcomes[name]}"
            for name in ("created", "updated", "unchanged", "conflict")
        )
    )
    if conflicts:
        for conflict in conflicts:
            print(f"冲突：{conflict}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
