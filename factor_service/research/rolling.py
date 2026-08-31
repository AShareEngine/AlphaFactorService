from __future__ import annotations

from datetime import date
from typing import Any, Mapping


def effective_rolling_window(
    walk_forward: Mapping[str, Any] | None,
    trade_date: str,
) -> dict[str, Any] | None:
    """Resolve exactly one immutable rolling window for a signal date."""

    contract = dict(walk_forward or {})
    if contract.get("enabled") is not True:
        return None
    try:
        target = date.fromisoformat(str(trade_date)[:10])
    except ValueError as exc:
        raise ValueError("滚动模型推理日期必须是ISO日期") from exc

    matches: list[dict[str, Any]] = []
    for source in contract.get("windows") or []:
        window = dict(source)
        try:
            start = date.fromisoformat(str(window["effective_date_start"])[:10])
            end = date.fromisoformat(str(window["effective_date_end"])[:10])
        except (KeyError, ValueError) as exc:
            raise ValueError("滚动模型窗口缺少有效的生效日期") from exc
        if start > end:
            raise ValueError("滚动模型窗口生效区间无效")
        if start <= target <= end:
            matches.append(window)

    if not matches:
        start = str(contract.get("prediction_date_start") or "")[:10]
        end = str(contract.get("prediction_date_end") or "")[:10]
        coverage = f"{start}至{end}" if start and end else "未声明"
        raise ValueError(
            f"{trade_date}不在滚动模型序列覆盖范围内（{coverage}）；"
            "必须先训练并发布覆盖该日期的新滚动窗口"
        )
    if len(matches) != 1:
        raise ValueError(f"{trade_date}同时命中多个滚动模型窗口，序列合同无效")
    return matches[0]


__all__ = ["effective_rolling_window"]
