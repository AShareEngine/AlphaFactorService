#!/usr/bin/env python3
"""Export JoinQuant's public factor catalog and detail metadata to Markdown."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://www.joinquant.com"
LIST_PAGE = f"{BASE_URL}/view/factorlib/list"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
)
SNAPSHOT_FILTERS = {
    "categoryId": "0",
    "universeType": "zz500",
    "timeRange": "3y",
    "commisionFee": "0",
    "skipPaused": "1",
}


def fetch_json(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = f"?{urlencode(params)}" if params else ""
    request = Request(
        f"{BASE_URL}{path}{query}",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": LIST_PAGE,
            "User-Agent": USER_AGENT,
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("code") != "00000":
                raise RuntimeError(payload.get("msg") or f"JoinQuant code={payload.get('code')}")
            return payload
        except Exception as exc:  # pragma: no cover - network retry path
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"请求聚宽接口失败: {path}: {last_error}") from last_error


def clean(value: Any, fallback: str = "-") -> str:
    text = " ".join(str(value or "").split())
    return text or fallback


def table_text(value: Any, fallback: str = "-") -> str:
    return clean(value, fallback).replace("|", r"\|")


def detail_url(factor_id: str) -> str:
    return f"{BASE_URL}/view/factorlib/detail/{factor_id}"


def fetch_detail(item: dict[str, Any]) -> dict[str, Any]:
    payload = fetch_json(
        "/factorlib/index/getInfo",
        {"id": item["factor_id"], "isFactorShare": "0"},
    )
    detail = payload.get("data") or {}
    return {
        **item,
        **detail,
        "snapshot_factor_id": item["factor_id"],
        "detail_fetch_ok": bool(detail),
    }


def performance_text(item: dict[str, Any]) -> str:
    fields = [
        ("IC 均值", "ic_mean"),
        ("IR", "ir"),
        ("多空年化", "annual_return_ls"),
        ("多空夏普", "sharpe_ls"),
        ("多空最大回撤", "max_drawdown_ls"),
    ]
    values = []
    for label, key in fields:
        value = item.get(key)
        if value is not None and value != "":
            values.append(f"{label}={value}")
    return "；".join(values) or "当前公开响应未返回绩效数值"


def category_sort_key(name: str, order: dict[str, int]) -> tuple[int, str]:
    return order.get(name, len(order)), name


def render_markdown(
    items: list[dict[str, Any]],
    categories: list[dict[str, Any]],
    settings: dict[str, Any],
    generated_at: datetime,
) -> str:
    category_order = {
        clean(category.get("intro")): index for index, category in enumerate(categories)
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[clean(item.get("categoryName"), "未分类")].append(item)
    for group in grouped.values():
        group.sort(key=lambda row: clean(row.get("name")).lower())

    category_names = sorted(
        grouped,
        key=lambda name: category_sort_key(name, category_order),
    )
    detail_success = sum(bool(item.get("detail_fetch_ok")) for item in items)
    formula_count = sum(clean(item.get("algorithmIntro"), "") != "" for item in items)
    category_counts = Counter(clean(item.get("categoryName"), "未分类") for item in items)
    lines = [
        "# 聚宽因子库目录与详情快照",
        "",
        "> 本文档用于 AlphaFactorService 的因子迁移与实施分析。内容来自聚宽公开因子看板接口，",
        "> 是元数据与自然语言计算说明的快照，不代表已经取得聚宽的可执行源码或历史因子值。",
        "",
        "## 快照信息",
        "",
        f"- 生成时间：{generated_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- 来源页面：[{LIST_PAGE}]({LIST_PAGE})",
        "- 列表接口：`GET /factorlib/index/getList`",
        "- 详情接口：`GET /factorlib/index/getInfo`",
        "- 快照筛选：中证500、近3年、无佣金、跳过停牌",
        f"- 因子总数：{len(items)}",
        f"- 成功取得详情：{detail_success}/{len(items)}",
        f"- 带计算逻辑说明：{formula_count}/{len(items)}",
        "- 重新生成：`rtk .venv/bin/python scripts/export_joinquant_factor_catalog.py`",
        "",
        "### 重要实施约束",
        "",
        "1. 聚宽列表返回的 `factor_id` 会随股票池、时间范围等筛选条件变化，本文只记录快照 ID；正式导入应以英文因子名作为外部稳定标识。",
        "2. `algorithmIntro` 多数是自然语言说明，不是可直接执行的 QLib/Python 源码；迁移时必须单独翻译、校验和回测对齐。",
        "3. 财务、分析师预测、行业、市值暴露等因子依赖的数据不一定存在于当前股票日线源中，不能直接批量发布到正式因子库。",
        "4. `processMethods` 记录了聚宽的去极值、中性化和标准化流程；这部分与原始公式同样影响最终因子值。",
        "5. 详情页链接包含快照 ID，未来若聚宽重建 ID，旧链接可能失效。",
        "",
        "## 分类统计",
        "",
        "| 分类 | 因子数量 |",
        "|---|---:|",
    ]
    for category in category_names:
        lines.append(f"| {table_text(category)} | {category_counts[category]} |")

    universe = settings.get("universeType", {}).get("list", {})
    time_ranges = settings.get("timeRange", {}).get("list", {})
    lines.extend(
        [
            "",
            "## 聚宽看板参数快照",
            "",
            f"- 股票池：{clean(universe)}",
            f"- 时间范围：{clean(time_ranges)}",
            "",
            "## 因子总目录",
            "",
        ]
    )

    index_by_identity: dict[int, int] = {}
    ordinal = 1
    for category in category_names:
        lines.extend(
            [
                f"### {category}",
                "",
                "| # | 英文名 | 中文名 | 计算逻辑摘要 | 详情 |",
                "|---:|---|---|---|---|",
            ]
        )
        for item in grouped[category]:
            index_by_identity[id(item)] = ordinal
            logic = table_text(item.get("algorithmIntro"))
            if len(logic) > 90:
                logic = f"{logic[:87]}…"
            lines.append(
                f"| {ordinal} | `{table_text(item.get('name'))}` | "
                f"{table_text(item.get('intro'))} | {logic} | [查看](#factor-{ordinal:03d}) |"
            )
            ordinal += 1
        lines.append("")

    lines.extend(["## 因子详情", ""])
    for category in category_names:
        lines.extend([f"### {category}", ""])
        for item in grouped[category]:
            number = index_by_identity[id(item)]
            factor_id = clean(item.get("snapshot_factor_id"), "")
            name = clean(item.get("name"), "未命名")
            intro = clean(item.get("intro"), "未提供中文说明")
            logic = clean(item.get("algorithmIntro"), "聚宽公开详情未提供计算逻辑")
            lines.extend(
                [
                    f'<a id="factor-{number:03d}"></a>',
                    f"#### {number}. `{name}` — {intro}",
                    "",
                    f"- 聚宽分类：{clean(item.get('categoryName'), '未分类')}",
                    f"- 快照 factor_id：`{factor_id}`",
                    f"- 更新时间：{clean(item.get('update'))}",
                    f"- 产出时间：{clean(item.get('produceTime'))}",
                    f"- 数据处理：{clean(item.get('processMethods'), '未说明')}",
                    f"- 默认参数/加权：{clean(item.get('processParams'), '未说明')}",
                    f"- 看板绩效：{performance_text(item)}",
                    f"- 聚宽详情页：[打开快照详情]({detail_url(factor_id)})",
                    "- AlphaFactor 实施状态：`待转换评估`",
                    "- 依赖字段：`待梳理`",
                    "- 目标表达式：`待编写`",
                    "",
                    "计算逻辑：",
                    "",
                    f"> {logic}",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="docs/joinquant-factor-catalog.md",
        help="Markdown output path relative to the repository root.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Maximum concurrent detail requests.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    categories = fetch_json("/factorlib/index/getFctCategoryList").get("data") or []
    settings = fetch_json("/factorlib/index/getSetting").get("data") or {}
    factor_list = fetch_json("/factorlib/index/getList", SNAPSHOT_FILTERS).get("data") or []
    if not factor_list:
        raise RuntimeError("聚宽因子列表为空，停止生成文档")

    details: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 12))) as executor:
        futures = {executor.submit(fetch_detail, item): item for item in factor_list}
        for future in as_completed(futures):
            item = futures[future]
            try:
                details.append(future.result())
            except Exception as exc:
                details.append(
                    {
                        **item,
                        "snapshot_factor_id": item.get("factor_id", ""),
                        "detail_fetch_ok": False,
                        "detail_error": str(exc),
                        "categoryName": "未分类",
                    }
                )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_markdown(details, categories, settings, datetime.now().astimezone()),
        encoding="utf-8",
    )
    print(f"已生成 {output}: {len(details)} 个因子")


if __name__ == "__main__":
    main()
