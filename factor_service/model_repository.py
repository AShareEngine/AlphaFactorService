from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import re
from typing import Any, Mapping, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from factor_service import repository as factor_repository
from factor_service.clickhouse import client, settings
from factor_service.factor_backtest import UNIVERSES
from factor_service.schemas import (
    ModelBacktestDailyOut,
    ModelBacktestJobCreate,
    ModelBacktestJobOut,
    ModelPredictionBatchIn,
    ModelPredictionOut,
    ModelSignalOut,
)
from factor_service.worker import factor_params_hash


ARCHITECTURE_ABLATION_PROFILES: dict[str, dict[str, Any]] = {
    "stock_only": {
        "label": "仅个股",
        "stages": {"stock_rank"},
        "pipeline_mode": "flat",
    },
    "style_stock": {
        "label": "风格 + 个股",
        "stages": {"style_gate", "stock_rank"},
        "pipeline_mode": "hierarchical",
    },
    "industry_stock": {
        "label": "行业 + 个股",
        "stages": {"industry_gate", "stock_rank"},
        "pipeline_mode": "hierarchical",
    },
    "full": {
        "label": "三级全开",
        "stages": {"style_gate", "industry_gate", "risk_gate", "stock_rank"},
        "pipeline_mode": "hierarchical",
    },
}


def architecture_ablation_profiles() -> list[dict[str, Any]]:
    return [
        {"key": key, "label": str(value["label"])}
        for key, value in ARCHITECTURE_ABLATION_PROFILES.items()
    ]


def _architecture_backtest_walk_forward_contract(
    engines: list[Mapping[str, Any]],
) -> dict[str, Any]:
    signatures = [
        tuple(
            (
                str(window.get("test_start") or ""),
                str(window.get("test_end") or ""),
            )
            for window in (item.get("walk_forward") or {}).get("windows") or []
        )
        for item in engines
    ]
    all_enabled = bool(engines) and all(
        (item.get("walk_forward") or {}).get("enabled") is True
        for item in engines
    )
    aligned = (
        all_enabled and bool(signatures) and bool(signatures[0])
        and len(set(signatures)) == 1
    )
    return {
        "eligible": aligned,
        "source_count": len(engines),
        "window_count": len(signatures[0]) if aligned else 0,
        "strategy": str(
            ((engines[0].get("walk_forward") or {}).get("strategy") or "rolling")
        ) if engines else "rolling",
        "windows": [
            {"window": index, "test_start": start, "test_end": end}
            for index, (start, end) in enumerate(signatures[0], start=1)
        ] if aligned else [],
        "reason": (
            "" if aligned
            else "至少一个消融引擎不是Walk-Forward模型或测试窗口未对齐"
        ),
        "policy": "alphablocks.architecture-walk-forward.v1",
    }


def insert_model_predictions(payload: ModelPredictionBatchIn) -> int:
    database = settings().model_database
    now = datetime.now()
    rows = [
        [
            row.trade_date, "stock", row.entity_code, payload.model_id,
            payload.model_version, row.raw_prediction, row.rank_value,
            row.percentile, row.score, row.feature_cutoff_at, row.computed_at,
            row.source_vintage, payload.dataset_hash, payload.inference_run_id, now,
        ]
        for row in payload.rows
    ]
    client().insert(
        f"{database}.model_predictions_daily",
        rows,
        column_names=[
            "trade_date", "entity_type", "entity_code", "model_id",
            "model_version", "raw_prediction", "rank_value", "percentile",
            "score", "feature_cutoff_at", "computed_at", "source_vintage",
            "dataset_hash", "inference_run_id", "updated_at",
        ],
    )
    return len(rows)


def list_model_predictions(
    *, model_id: str, model_version: int, trade_date: Optional[date] = None,
    entity_code: str = "", limit: int = 500,
) -> list[ModelPredictionOut]:
    database = settings().model_database
    clean_entity_code = str(entity_code or "").strip()
    if trade_date:
        date_condition = "AND trade_date = {trade_date:Date}"
    elif clean_entity_code:
        # 单标的研究需要返回跨日期历史；普通截面查询仍只取最新交易日。
        date_condition = ""
    else:
        date_condition = f"""
          AND trade_date = (
              SELECT max(trade_date)
              FROM {database}.model_predictions_daily FINAL
              WHERE model_id = {{model_id:String}}
                AND model_version = {{model_version:UInt32}}
          )
        """
    entity_condition = "AND entity_code = {entity_code:String}" if clean_entity_code else ""
    params = {
        "model_id": model_id, "model_version": model_version,
        "limit": max(1, min(limit, 5000)),
    }
    if trade_date:
        params["trade_date"] = trade_date
    if clean_entity_code:
        params["entity_code"] = clean_entity_code
    rows = client().query(
        f"""
        SELECT trade_date, entity_code, raw_prediction, rank_value, percentile,
               score, feature_cutoff_at, computed_at, source_vintage,
               dataset_hash, inference_run_id
        FROM {database}.model_predictions_daily FINAL
        WHERE model_id = {{model_id:String}}
          AND model_version = {{model_version:UInt32}}
          {date_condition}
          {entity_condition}
        ORDER BY trade_date DESC, score DESC, entity_code
        LIMIT {{limit:UInt32}}
        """,
        parameters=params,
    ).result_rows
    return [
        ModelPredictionOut(
            trade_date=row[0], entity_code=row[1], raw_prediction=row[2],
            rank_value=row[3], percentile=row[4], score=row[5],
            feature_cutoff_at=row[6], computed_at=row[7], source_vintage=row[8],
            dataset_hash=row[9], inference_run_id=row[10],
            model_id=model_id, model_version=model_version,
        )
        for row in rows
    ]


def model_prediction_overview(
    *, model_id: str, model_version: int, trade_date: Optional[date] = None,
    top_n: int = 20, history_days: int = 120,
) -> dict[str, Any]:
    """Summarize one prediction cross-section and its day-over-day stability."""
    database = settings().model_database
    safe_history_days = max(2, min(int(history_days), 250))
    date_rows = client().query(
        f"""
        SELECT trade_date, count() AS row_count,
               countIf(feature_cutoff_at <= toDateTime(trade_date, 'Asia/Shanghai')
                       + INTERVAL 15 HOUR) AS pit_safe_rows,
               uniqExact(dataset_hash) AS dataset_count,
               uniqExact(inference_run_id) AS inference_run_count
        FROM {database}.model_predictions_daily FINAL
        WHERE model_id = {{model_id:String}}
          AND model_version = {{model_version:UInt32}}
        GROUP BY trade_date
        ORDER BY trade_date DESC
        LIMIT {{history_days:UInt32}}
        """,
        parameters={
            "model_id": model_id,
            "model_version": int(model_version),
            "history_days": safe_history_days,
        },
    ).result_rows
    if not date_rows:
        raise ValueError("模型没有可用预测截面")
    dates = [row[0] for row in date_rows]
    selected_date = trade_date or dates[0]
    if selected_date not in dates:
        raise ValueError(f"所选日期{selected_date}不在最近{safe_history_days}个预测日内")
    selected_index = dates.index(selected_date)
    previous_date = dates[selected_index + 1] if selected_index + 1 < len(dates) else None
    requested_dates = [selected_date]
    if previous_date is not None:
        requested_dates.append(previous_date)
    rows = client().query(
        f"""
        SELECT trade_date, entity_code, raw_prediction, rank_value, percentile,
               score, feature_cutoff_at, computed_at, dataset_hash,
               inference_run_id, source_vintage
        FROM {database}.model_predictions_daily FINAL
        WHERE model_id = {{model_id:String}}
          AND model_version = {{model_version:UInt32}}
          AND trade_date IN {{trade_dates:Array(Date)}}
        ORDER BY trade_date DESC, score DESC, entity_code
        """,
        parameters={
            "model_id": model_id,
            "model_version": int(model_version),
            "trade_dates": requested_dates,
        },
    ).result_rows
    frame = pd.DataFrame(rows, columns=[
        "trade_date", "entity_code", "raw_prediction", "rank_value",
        "percentile", "score", "feature_cutoff_at", "computed_at",
        "dataset_hash", "inference_run_id", "source_vintage",
    ])
    selected = frame[frame["trade_date"] == selected_date].copy()
    if selected.empty:
        raise ValueError("所选日期没有预测数据")
    for column in ["raw_prediction", "rank_value", "percentile", "score"]:
        selected[column] = pd.to_numeric(selected[column], errors="coerce")
    selected = selected.sort_values(
        ["score", "entity_code"], ascending=[False, True], na_position="last",
    )
    previous = frame[frame["trade_date"] == previous_date].copy()
    if not previous.empty:
        for column in ["rank_value", "score"]:
            previous[column] = pd.to_numeric(previous[column], errors="coerce")
    safe_top_n = max(1, min(int(top_n), 500))
    date_meta = {
        row[0]: {
            "trade_date": row[0],
            "row_count": int(row[1]),
            "pit_safe_rows": int(row[2]),
            "pit_violation_count": int(row[1]) - int(row[2]),
            "dataset_count": int(row[3]),
            "inference_run_count": int(row[4]),
        }
        for row in date_rows
    }
    top_candidates = [
        {
            "entity_code": str(row.entity_code),
            "rank_value": int(row.rank_value) if pd.notna(row.rank_value) else None,
            "percentile": float(row.percentile) if pd.notna(row.percentile) else None,
            "score": float(row.score) if pd.notna(row.score) else None,
            "raw_prediction": (
                float(row.raw_prediction) if pd.notna(row.raw_prediction) else None
            ),
        }
        for row in selected.head(safe_top_n).itertuples(index=False)
    ]
    dataset_hashes = sorted(
        str(value) for value in selected["dataset_hash"].dropna().unique()
    )
    inference_runs = sorted(
        str(value) for value in selected["inference_run_id"].dropna().unique()
    )
    return {
        "model_id": model_id,
        "model_version": int(model_version),
        "selected_date": selected_date,
        "available_dates": [
            {
                **date_meta[value],
                "is_selected": value == selected_date,
            }
            for value in dates
        ],
        "cross_section": {
            **date_meta[selected_date],
            "missing_score_count": int(selected["score"].isna().sum()),
            "duplicate_entity_count": int(
                selected["entity_code"].duplicated(keep=False).sum()
            ),
            "dataset_hash": dataset_hashes[0] if len(dataset_hashes) == 1 else "",
            "dataset_hashes": dataset_hashes,
            "inference_run_id": inference_runs[0] if len(inference_runs) == 1 else "",
            "inference_run_ids": inference_runs,
            "feature_cutoff_at": _timestamp_max(selected["feature_cutoff_at"]),
            "computed_at": _timestamp_max(selected["computed_at"]),
        },
        "score_stats": _prediction_number_stats(selected["score"]),
        "raw_prediction_stats": _prediction_number_stats(
            selected["raw_prediction"],
        ),
        "score_histogram": _prediction_score_histogram(selected["score"]),
        "score_bands": _prediction_score_bands(selected),
        "top_n": safe_top_n,
        "top_candidates": top_candidates,
        "stability": _prediction_rank_stability(
            selected, previous, selected_date=selected_date,
            previous_date=previous_date, top_n=safe_top_n,
        ),
    }


def _prediction_number_stats(values: pd.Series) -> dict[str, Any]:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return {
            "count": 0, "mean": None, "std": None, "min": None, "max": None,
            "p10": None, "p25": None, "p50": None, "p75": None, "p90": None,
        }
    return {
        "count": int(numeric.size),
        "mean": float(numeric.mean()),
        "std": float(numeric.std(ddof=0)),
        "min": float(numeric.min()),
        "max": float(numeric.max()),
        "p10": float(numeric.quantile(0.10)),
        "p25": float(numeric.quantile(0.25)),
        "p50": float(numeric.quantile(0.50)),
        "p75": float(numeric.quantile(0.75)),
        "p90": float(numeric.quantile(0.90)),
    }


def _prediction_score_histogram(values: pd.Series) -> list[dict[str, Any]]:
    numeric = pd.to_numeric(values, errors="coerce").dropna().clip(-1.0, 1.0)
    edges = np.linspace(-1.0, 1.0, 11)
    counts, _ = np.histogram(numeric.to_numpy(), bins=edges)
    return [
        {
            "lower": float(edges[index]),
            "upper": float(edges[index + 1]),
            "label": f"{edges[index]:.1f}~{edges[index + 1]:.1f}",
            "count": int(count),
            "ratio": float(count / len(numeric)) if len(numeric) else 0.0,
        }
        for index, count in enumerate(counts)
    ]


def _prediction_score_bands(frame: pd.DataFrame) -> list[dict[str, Any]]:
    bands = [
        ("top_10", "Top 10%", 0.90, 1.01),
        ("top_20", "Top 10%~20%", 0.80, 0.90),
        ("upper_middle", "Top 20%~50%", 0.50, 0.80),
        ("lower_middle", "Bottom 20%~50%", 0.20, 0.50),
        ("bottom_20", "Bottom 20%", -0.01, 0.20),
    ]
    rows = []
    for key, label, lower, upper in bands:
        group = frame[
            (frame["percentile"] >= lower) & (frame["percentile"] < upper)
        ]
        rows.append({
            "key": key,
            "label": label,
            "count": int(len(group)),
            "ratio": float(len(group) / len(frame)) if len(frame) else 0.0,
            "score_mean": _series_mean(group["score"]),
            "raw_prediction_mean": _series_mean(group["raw_prediction"]),
            "rank_min": _series_min(group["rank_value"]),
            "rank_max": _series_max(group["rank_value"]),
        })
    return rows


def _prediction_rank_stability(
    selected: pd.DataFrame, previous: pd.DataFrame, *, selected_date: date,
    previous_date: Optional[date], top_n: int,
) -> dict[str, Any]:
    empty = {
        "available": False,
        "selected_date": selected_date,
        "previous_date": previous_date,
        "common_entities": 0,
        "rank_correlation": None,
        "mean_absolute_rank_change": None,
        "top_n": top_n,
        "comparison_top_n": 0,
        "top_n_overlap_count": 0,
        "top_n_overlap_ratio": None,
        "new_entrants": [],
        "exits": [],
    }
    if previous.empty or previous_date is None:
        return empty
    current = selected[["entity_code", "rank_value", "score"]].rename(columns={
        "rank_value": "current_rank", "score": "current_score",
    })
    prior = previous[["entity_code", "rank_value", "score"]].rename(columns={
        "rank_value": "previous_rank", "score": "previous_score",
    })
    aligned = current.merge(prior, on="entity_code", how="inner").dropna(
        subset=["current_rank", "previous_rank"],
    )
    correlation = None
    mean_rank_change = None
    if not aligned.empty:
        correlation_value = aligned["current_rank"].corr(
            aligned["previous_rank"], method="spearman",
        )
        correlation = float(correlation_value) if pd.notna(correlation_value) else None
        mean_rank_change = float(
            (aligned["current_rank"] - aligned["previous_rank"]).abs().mean()
        )
    comparison_top_n = min(top_n, len(selected), len(previous))
    current_top = set(
        selected.nsmallest(comparison_top_n, "rank_value")["entity_code"].astype(str)
    )
    previous_top = set(
        previous.nsmallest(comparison_top_n, "rank_value")["entity_code"].astype(str)
    )
    overlap = current_top & previous_top
    return {
        **empty,
        "available": True,
        "common_entities": int(len(aligned)),
        "rank_correlation": correlation,
        "mean_absolute_rank_change": mean_rank_change,
        "comparison_top_n": comparison_top_n,
        "top_n_overlap_count": len(overlap),
        "top_n_overlap_ratio": (
            len(overlap) / comparison_top_n if comparison_top_n else None
        ),
        "new_entrants": sorted(current_top - previous_top),
        "exits": sorted(previous_top - current_top),
    }


def _series_mean(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return float(numeric.mean()) if not numeric.empty else None


def _series_min(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return float(numeric.min()) if not numeric.empty else None


def _series_max(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return float(numeric.max()) if not numeric.empty else None


def _timestamp_max(values: pd.Series) -> str:
    timestamps = pd.to_datetime(values, errors="coerce")
    if timestamps.dropna().empty:
        return ""
    return timestamps.max().isoformat()


def list_model_signals(
    *, model_id: str, model_version: int, trade_date: date,
    top_n: int = 20,
) -> list[ModelSignalOut]:
    """Read one immutable daily signal snapshot for AlphaBlocks strategy backtests."""
    rows = client().query(
        f"""
        SELECT trade_date, entity_code, score, rank_value, feature_cutoff_at,
               dataset_hash, inference_run_id
        FROM {settings().model_database}.model_predictions_daily FINAL
        WHERE model_id = {{model_id:String}}
          AND model_version = {{model_version:UInt32}}
          AND trade_date = {{trade_date:Date}}
          AND feature_cutoff_at <= toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR
        ORDER BY score DESC, entity_code
        LIMIT {{top_n:UInt32}}
        """,
        parameters={
            "model_id": model_id, "model_version": int(model_version),
            "trade_date": trade_date, "top_n": max(1, min(int(top_n), 500)),
        },
    ).result_rows
    return [
        ModelSignalOut(
            trade_date=row[0], entity_code=row[1], score=row[2], rank_value=row[3],
            feature_cutoff_at=row[4], dataset_hash=row[5], inference_run_id=row[6],
        )
        for row in rows
    ]


def ensemble_prediction_availability(
    *, sources: list[Mapping[str, Any]], requested_trade_date: Optional[date] = None,
) -> dict[str, Any]:
    """Return dates where every immutable source model has a PIT-safe prediction."""
    condition, parameters = _ensemble_source_condition(sources)
    requested_column = ""
    if requested_trade_date is not None:
        requested_column = (
            ", max(if(trade_date = {requested_trade_date:Date}, row_count, 0))"
            " AS requested_row_count"
        )
        parameters["requested_trade_date"] = requested_trade_date
    database = settings().model_database
    row = client().query(
        f"""
        SELECT max(trade_date), min(trade_date), count(), sum(row_count)
               {requested_column}
        FROM (
            SELECT trade_date, count() AS row_count
            FROM (
                SELECT trade_date, entity_code
                FROM (
                    SELECT model_id, model_version, trade_date, entity_code
                    FROM {database}.model_predictions_daily FINAL
                    WHERE ({condition})
                      AND feature_cutoff_at <=
                          toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR
                    GROUP BY model_id, model_version, trade_date, entity_code
                )
                GROUP BY trade_date, entity_code
                HAVING uniqExact(tuple(model_id, model_version)) =
                       {{source_count:UInt32}}
            )
            GROUP BY trade_date
        )
        """,
        parameters=parameters,
    ).result_rows[0]
    requested_rows = int(row[4] or 0) if requested_trade_date is not None else None
    return {
        "trade_date": row[0],
        "date_start": row[1],
        "date_count": int(row[2] or 0),
        "row_count": int(row[3] or 0),
        "source_count": len(sources),
        "requested_trade_date": requested_trade_date,
        "requested_trade_date_available": (
            requested_rows > 0 if requested_trade_date is not None else None
        ),
        "requested_row_count": requested_rows,
    }


def ensemble_prediction_dates(
    *, sources: list[Mapping[str, Any]], after_date: date,
    before_date: Optional[date] = None, limit: int = 20,
) -> list[date]:
    """Return ordered dates where all source model versions have safe predictions."""
    condition, parameters = _ensemble_source_condition(sources)
    parameters.update({
        "after_date": after_date,
        "before_date": before_date or date.max,
        "limit": max(1, min(int(limit), 250)),
    })
    database = settings().model_database
    rows = client().query(
        f"""
        SELECT trade_date
        FROM (
            SELECT trade_date, entity_code
            FROM (
                SELECT model_id, model_version, trade_date, entity_code
                FROM {database}.model_predictions_daily FINAL
                WHERE ({condition})
                  AND trade_date > {{after_date:Date}}
                  AND trade_date <= {{before_date:Date}}
                  AND feature_cutoff_at <=
                      toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR
                GROUP BY model_id, model_version, trade_date, entity_code
            )
            GROUP BY trade_date, entity_code
            HAVING uniqExact(tuple(model_id, model_version)) =
                   {{source_count:UInt32}}
        )
        GROUP BY trade_date
        ORDER BY trade_date
        LIMIT {{limit:UInt32}}
        """,
        parameters=parameters,
    ).result_rows
    return [row[0] for row in rows]


def model_prediction_comparison(
    *, sources: list[Mapping[str, Any]], horizon: int | None = None,
) -> dict[str, Any]:
    """Compare immutable model scores on their common PIT-safe OOS cross-sections."""
    condition, parameters = _ensemble_source_condition(sources)
    database = settings().model_database
    rows = client().query(
        f"""
        SELECT model_id, model_version, trade_date, entity_code,
               argMax(score, tuple(updated_at, computed_at, inference_run_id))
                   AS source_score
        FROM {database}.model_predictions_daily FINAL
        WHERE ({condition})
          AND feature_cutoff_at <=
              toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR
        GROUP BY model_id, model_version, trade_date, entity_code
        ORDER BY trade_date, entity_code, model_id, model_version
        """,
        parameters=parameters,
    ).result_rows
    source_views = [_diagnostic_source_view(source) for source in sources]
    source_keys = [str(item["source_key"]) for item in source_views]
    if not rows:
        return {
            "sources": source_views,
            "common_rows": 0,
            "common_days": 0,
            "date_start": None,
            "date_end": None,
            "correlation_matrix": [],
            "evaluation_rows": 0,
            "evaluation_days": 0,
            "metrics": [],
        }
    long = pd.DataFrame(
        rows,
        columns=["model_id", "model_version", "trade_date", "instrument", "score"],
    )
    long["trade_date"] = pd.to_datetime(long["trade_date"])
    long["score"] = pd.to_numeric(long["score"], errors="coerce")
    long["source_key"] = long.apply(
        lambda row: _source_key(str(row["model_id"]), int(row["model_version"])),
        axis=1,
    )
    pivot = long.pivot_table(
        index=["trade_date", "instrument"], columns="source_key",
        values="score", aggfunc="last",
    )
    if any(key not in pivot.columns for key in source_keys):
        common = pd.DataFrame(columns=["trade_date", "instrument", *source_keys])
    else:
        common = pivot[source_keys].dropna().reset_index()
    if common.empty:
        matrix: list[list[float | None]] = []
        date_start = None
        date_end = None
        evaluation_rows = 0
        evaluation_days = 0
        metrics: list[dict[str, Any]] = []
    else:
        matrix = []
        for left, left_key in enumerate(source_keys):
            matrix_row: list[float | None] = []
            for right, right_key in enumerate(source_keys):
                if left == right:
                    matrix_row.append(1.0)
                    continue
                daily: list[float] = []
                for _, group in common.groupby("trade_date", sort=True):
                    if group[left_key].nunique() <= 1 or group[right_key].nunique() <= 1:
                        continue
                    value = group[left_key].corr(group[right_key], method="spearman")
                    if pd.notna(value):
                        daily.append(float(value))
                matrix_row.append(float(np.mean(daily)) if daily else None)
            matrix.append(matrix_row)
        date_start = common["trade_date"].min().date()
        date_end = common["trade_date"].max().date()
        evaluation_rows = 0
        evaluation_days = 0
        metrics = []
        if horizon is not None:
            labels = _realized_label_frame(
                instruments=sorted(common["instrument"].astype(str).unique()),
                date_start=date_start,
                date_end=date_end,
                horizon=max(1, int(horizon)),
            )
            evaluated = common.merge(
                labels, on=["trade_date", "instrument"], how="inner",
            ).dropna(subset=[*source_keys, "label"])
            evaluation_rows = int(len(evaluated))
            evaluation_days = int(evaluated["trade_date"].nunique())
            for source, key in zip(source_views, source_keys, strict=True):
                score = _rank_ensemble_score(
                    evaluated, [key], np.asarray([1.0]),
                )
                metrics.append({
                    **source,
                    **_daily_rank_metrics(evaluated, score),
                })
    return {
        "sources": source_views,
        "common_rows": int(len(common)),
        "common_days": int(common["trade_date"].nunique()) if not common.empty else 0,
        "date_start": date_start,
        "date_end": date_end,
        "correlation_matrix": matrix,
        "evaluation_rows": evaluation_rows,
        "evaluation_days": evaluation_days,
        "metrics": metrics,
    }


def model_prediction_reproducibility_audit(
    *, source_model_id: str, source_model_version: int,
    replay_model_id: str, replay_model_version: int,
) -> dict[str, Any]:
    """Compare two immutable prediction sets without consulting future labels.

    A reproducibility audit is stricter than the regular model comparison: it
    requires the same PIT-safe date/entity keys and checks both raw predictions
    and published cross-sectional scores.  Tiny floating-point differences are
    reported separately from material drift.
    """
    sources = [
        {"model_id": source_model_id, "model_version": int(source_model_version)},
        {"model_id": replay_model_id, "model_version": int(replay_model_version)},
    ]
    condition, parameters = _ensemble_source_condition(sources)
    database = settings().model_database
    rows = client().query(
        f"""
        SELECT model_id, model_version, trade_date, entity_code,
               argMax(raw_prediction, tuple(updated_at, computed_at, inference_run_id))
                   AS raw_prediction,
               argMax(score, tuple(updated_at, computed_at, inference_run_id))
                   AS score
        FROM {database}.model_predictions_daily FINAL
        WHERE ({condition})
          AND feature_cutoff_at <=
              toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR
        GROUP BY model_id, model_version, trade_date, entity_code
        ORDER BY trade_date, entity_code, model_id, model_version
        """,
        parameters=parameters,
    ).result_rows
    keys = [
        _source_key(source_model_id, int(source_model_version)),
        _source_key(replay_model_id, int(replay_model_version)),
    ]
    empty = {
        "source_rows": 0,
        "replay_rows": 0,
        "common_rows": 0,
        "source_only_rows": 0,
        "replay_only_rows": 0,
        "common_days": 0,
        "date_start": None,
        "date_end": None,
        "key_set_equal": False,
        "raw_prediction": _numeric_reproducibility_stats(pd.Series(dtype=float), pd.Series(dtype=float)),
        "score": _numeric_reproducibility_stats(pd.Series(dtype=float), pd.Series(dtype=float)),
        "status": "unavailable",
        "passed": False,
    }
    if not rows:
        return empty
    frame = pd.DataFrame(rows, columns=[
        "model_id", "model_version", "trade_date", "entity_code",
        "raw_prediction", "score",
    ])
    frame["source_key"] = frame.apply(
        lambda row: _source_key(str(row["model_id"]), int(row["model_version"])),
        axis=1,
    )
    source = frame.loc[frame["source_key"] == keys[0], [
        "trade_date", "entity_code", "raw_prediction", "score",
    ]].rename(columns={
        "raw_prediction": "source_raw", "score": "source_score",
    })
    replay = frame.loc[frame["source_key"] == keys[1], [
        "trade_date", "entity_code", "raw_prediction", "score",
    ]].rename(columns={
        "raw_prediction": "replay_raw", "score": "replay_score",
    })
    aligned = source.merge(
        replay, on=["trade_date", "entity_code"], how="outer", indicator=True,
    )
    common = aligned.loc[aligned["_merge"] == "both"].copy()
    source_only = int((aligned["_merge"] == "left_only").sum())
    replay_only = int((aligned["_merge"] == "right_only").sum())
    key_set_equal = source_only == 0 and replay_only == 0 and not common.empty
    raw = _numeric_reproducibility_stats(common["source_raw"], common["replay_raw"])
    score = _numeric_reproducibility_stats(common["source_score"], common["replay_score"])
    exact = (
        key_set_equal
        and raw["max_absolute_delta"] is not None
        and raw["max_absolute_delta"] <= 1e-12
        and score["max_absolute_delta"] is not None
        and score["max_absolute_delta"] <= 1e-12
    )
    equivalent = (
        key_set_equal
        and raw["correlation"] is not None
        and raw["correlation"] >= 0.999999
        and score["correlation"] is not None
        and score["correlation"] >= 0.999999
        and score["max_absolute_delta"] is not None
        and score["max_absolute_delta"] <= 1e-8
    )
    if exact:
        status = "exact"
    elif equivalent:
        status = "equivalent"
    else:
        status = "drifted"
    return {
        **empty,
        "source_rows": int(len(source)),
        "replay_rows": int(len(replay)),
        "common_rows": int(len(common)),
        "source_only_rows": source_only,
        "replay_only_rows": replay_only,
        "common_days": int(common["trade_date"].nunique()) if not common.empty else 0,
        "date_start": common["trade_date"].min() if not common.empty else None,
        "date_end": common["trade_date"].max() if not common.empty else None,
        "key_set_equal": key_set_equal,
        "raw_prediction": raw,
        "score": score,
        "status": status,
        "passed": status in {"exact", "equivalent"},
    }


def _numeric_reproducibility_stats(
    source: pd.Series, replay: pd.Series,
) -> dict[str, Any]:
    left = pd.to_numeric(source, errors="coerce")
    right = pd.to_numeric(replay, errors="coerce")
    valid = left.notna() & right.notna()
    left = left.loc[valid].astype(float)
    right = right.loc[valid].astype(float)
    if left.empty:
        return {
            "compared_rows": 0,
            "mean_absolute_delta": None,
            "max_absolute_delta": None,
            "correlation": None,
            "identical_ratio": None,
        }
    delta = (left - right).abs()
    if len(left) == 1:
        correlation = 1.0 if float(delta.iloc[0]) <= 1e-12 else None
    elif left.nunique() <= 1 and right.nunique() <= 1:
        correlation = 1.0 if float(delta.max()) <= 1e-12 else None
    else:
        value = left.corr(right, method="pearson")
        correlation = float(value) if pd.notna(value) else None
    return {
        "compared_rows": int(len(left)),
        "mean_absolute_delta": float(delta.mean()),
        "max_absolute_delta": float(delta.max()),
        "correlation": correlation,
        "identical_ratio": float((delta <= 1e-12).mean()),
    }


def materialize_ensemble_predictions(
    *, model_id: str, model_version: int, sources: list[Mapping[str, Any]],
    dataset_hash: str, inference_run_prefix: str,
    trade_date: Optional[date] = None,
) -> dict[str, Any]:
    """Fuse source scores in ClickHouse and re-rank every common cross-section."""
    condition, parameters = _ensemble_source_condition(sources)
    weight_cases: list[str] = []
    for index, source in enumerate(sources):
        weight_key = f"source_weight_{index}"
        parameters[weight_key] = float(source["weight"])
        weight_cases.extend([
            (
                f"model_id = {{source_model_id_{index}:String}} AND "
                f"model_version = {{source_model_version_{index}:UInt32}}"
            ),
            f"{{{weight_key}:Float64}}",
        ])
    weight_expression = f"multiIf({', '.join(weight_cases)}, 0.0)"
    date_condition = ""
    if trade_date is not None:
        date_condition = "AND trade_date = {trade_date:Date}"
        parameters["trade_date"] = trade_date
    parameters.update({
        "target_model_id": str(model_id),
        "target_model_version": int(model_version),
        "dataset_hash": str(dataset_hash),
        "inference_run_prefix": str(inference_run_prefix),
        "source_vintage": f"ensemble:{dataset_hash[:24]}",
    })
    database = settings().model_database
    target = f"{database}.model_predictions_daily"
    client().command(
        f"""
        INSERT INTO {target} (
            trade_date, entity_type, entity_code, model_id, model_version,
            raw_prediction, rank_value, percentile, score,
            feature_cutoff_at, computed_at, source_vintage, dataset_hash,
            inference_run_id, updated_at
        )
        WITH source_rows AS (
            SELECT model_id, model_version, trade_date, entity_code,
                   argMax(score, tuple(updated_at, computed_at, inference_run_id))
                       AS source_score,
                   argMax(feature_cutoff_at,
                          tuple(updated_at, computed_at, inference_run_id))
                       AS source_feature_cutoff_at
            FROM {target} FINAL
            WHERE ({condition})
              {date_condition}
              AND feature_cutoff_at <=
                  toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR
            GROUP BY model_id, model_version, trade_date, entity_code
        ), fused AS (
            SELECT trade_date, entity_code,
                   sum(source_score * ({weight_expression})) /
                       sum({weight_expression}) AS raw_prediction,
                   max(source_feature_cutoff_at) AS feature_cutoff_at
            FROM source_rows
            GROUP BY trade_date, entity_code
            HAVING uniqExact(tuple(model_id, model_version)) =
                   {{source_count:UInt32}}
        ), ranked AS (
            SELECT *,
                   row_number() OVER (
                       PARTITION BY trade_date
                       ORDER BY raw_prediction DESC, entity_code
                   ) AS rank_value,
                   row_number() OVER (
                       PARTITION BY trade_date
                       ORDER BY raw_prediction ASC, entity_code DESC
                   ) AS ascending_rank,
                   count() OVER (PARTITION BY trade_date) AS section_count
            FROM fused
        ), normalized AS (
            SELECT *, if(
                section_count <= 1,
                0.5,
                (ascending_rank - 1.0) / (section_count - 1.0)
            ) AS percentile
            FROM ranked
        )
        SELECT trade_date, 'stock', entity_code,
               {{target_model_id:String}}, {{target_model_version:UInt32}},
               raw_prediction, toUInt32(rank_value), percentile,
               2.0 * percentile - 1.0,
               feature_cutoff_at, now('Asia/Shanghai'),
               {{source_vintage:String}}, {{dataset_hash:String}},
               concat({{inference_run_prefix:String}}, toString(trade_date)),
               now('Asia/Shanghai')
        FROM normalized
        """,
        parameters=parameters,
    )
    summary_parameters = {
        "model_id": str(model_id),
        "model_version": int(model_version),
        "inference_run_prefix": str(inference_run_prefix),
    }
    summary_date_condition = ""
    if trade_date is not None:
        summary_date_condition = "AND trade_date = {summary_trade_date:Date}"
        summary_parameters["summary_trade_date"] = trade_date
    summary = client().query(
        f"""
        SELECT count(), min(trade_date), max(trade_date), uniqExact(trade_date)
        FROM {target} FINAL
        WHERE model_id = {{model_id:String}}
          AND model_version = {{model_version:UInt32}}
          AND startsWith(inference_run_id, {{inference_run_prefix:String}})
          {summary_date_condition}
        """,
        parameters=summary_parameters,
    ).result_rows[0]
    if int(summary[0] or 0) <= 0:
        scope = f"{trade_date}" if trade_date else "共同历史区间"
        raise ValueError(f"源模型在{scope}没有完整重叠预测")
    latest_date = summary[2]
    latest_cross_section_rows = int(summary[0])
    if trade_date is None:
        latest_cross_section_rows = int(client().query(
            f"""
            SELECT count()
            FROM {target} FINAL
            WHERE model_id = {{model_id:String}}
              AND model_version = {{model_version:UInt32}}
              AND startsWith(inference_run_id, {{inference_run_prefix:String}})
              AND trade_date = {{latest_trade_date:Date}}
            """,
            parameters={
                **summary_parameters,
                "latest_trade_date": latest_date,
            },
        ).result_rows[0][0])
    return {
        "model_id": str(model_id),
        "model_version": int(model_version),
        "row_count": int(summary[0]),
        "date_start": summary[1],
        "date_end": latest_date,
        "latest_trade_date": latest_date,
        "date_count": int(summary[3]),
        "latest_cross_section_rows": latest_cross_section_rows,
        "inference_run_id": f"{inference_run_prefix}{latest_date}",
        "dataset_hash": str(dataset_hash),
        "source_count": len(sources),
    }


def evaluate_model_predictions(
    *, model_id: str, model_version: int, horizon: int = 5,
) -> dict[str, Any]:
    """Evaluate persisted OOS scores against realized future cross-sectional ranks."""
    database = settings().model_database
    rows = client().query(
        f"""
        SELECT trade_date, entity_code,
               argMax(score, tuple(updated_at, computed_at, inference_run_id)) AS prediction
        FROM {database}.model_predictions_daily FINAL
        WHERE model_id = {{model_id:String}}
          AND model_version = {{model_version:UInt32}}
        GROUP BY trade_date, entity_code
        ORDER BY trade_date, entity_code
        """,
        parameters={"model_id": model_id, "model_version": int(model_version)},
    ).result_rows
    predictions = pd.DataFrame(
        rows, columns=["trade_date", "instrument", "prediction"],
    )
    if predictions.empty:
        raise ValueError("模型没有可评价的样本外预测")
    predictions["trade_date"] = pd.to_datetime(predictions["trade_date"])
    predictions["prediction"] = pd.to_numeric(
        predictions["prediction"], errors="coerce",
    )
    instruments = sorted(predictions["instrument"].astype(str).unique())
    date_start = predictions["trade_date"].min().date()
    date_end = predictions["trade_date"].max().date()
    labels = _realized_label_frame(
        instruments=instruments,
        date_start=date_start,
        date_end=date_end,
        horizon=horizon,
    )
    aligned = predictions.merge(
        labels, on=["trade_date", "instrument"], how="inner",
    ).dropna(subset=["prediction", "label"])
    if aligned.empty:
        raise ValueError("模型预测无法与未来收益标签对齐")
    metrics = _daily_rank_metrics(aligned, aligned["prediction"])
    return {
        "test_rows": int(len(aligned)),
        "test_days": int(aligned["trade_date"].nunique()),
        **metrics,
        "label_horizon_trading_days": max(1, int(horizon)),
        "evaluation_source": "realized_future_cross_sectional_rank",
    }


def _pit_safe_model_prediction_frame(
    *, model_id: str, model_version: int,
) -> pd.DataFrame:
    database = settings().model_database
    rows = client().query(
        f"""
        WITH latest AS (
            SELECT trade_date, entity_code,
                   argMax(score, tuple(updated_at, computed_at, inference_run_id))
                       AS prediction,
                   argMax(feature_cutoff_at,
                          tuple(updated_at, computed_at, inference_run_id))
                       AS feature_cutoff_at
            FROM {database}.model_predictions_daily FINAL
            WHERE model_id = {{model_id:String}}
              AND model_version = {{model_version:UInt32}}
            GROUP BY trade_date, entity_code
        )
        SELECT trade_date, entity_code, prediction
        FROM latest
        WHERE feature_cutoff_at <=
              toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR
        ORDER BY trade_date, entity_code
        """,
        parameters={"model_id": model_id, "model_version": int(model_version)},
    ).result_rows
    predictions = pd.DataFrame(
        rows, columns=["trade_date", "instrument", "prediction"],
    )
    if predictions.empty:
        raise ValueError("模型没有PIT安全的样本外预测")
    predictions["trade_date"] = pd.to_datetime(predictions["trade_date"])
    predictions["prediction"] = pd.to_numeric(
        predictions["prediction"], errors="coerce",
    )
    predictions = predictions.dropna(subset=["prediction"])
    return predictions


def model_prediction_quantile_diagnostics(
    *, model_id: str, model_version: int, horizon: int = 5,
    quantiles: int = 10, sample_interval: int = 1,
) -> dict[str, Any]:
    """Evaluate PIT-safe OOS scores with equal-count realized-return buckets."""
    clean_quantiles = max(2, min(20, int(quantiles)))
    clean_horizon = max(1, int(horizon))
    clean_sample_interval = max(1, int(sample_interval))
    predictions = _pit_safe_model_prediction_frame(
        model_id=model_id, model_version=model_version,
    )
    labels = _realized_label_frame(
        instruments=sorted(predictions["instrument"].astype(str).unique()),
        date_start=predictions["trade_date"].min().date(),
        date_end=predictions["trade_date"].max().date(),
        horizon=clean_horizon,
    )
    aligned = predictions.merge(
        labels, on=["trade_date", "instrument"], how="inner",
    ).dropna(subset=["prediction", "label", "forward_return"])
    if aligned.empty:
        raise ValueError("模型预测无法与未来实际收益对齐")
    minimum_section = clean_quantiles * 5
    section_sizes = aligned.groupby("trade_date")["instrument"].transform("size")
    aligned = aligned.loc[section_sizes >= minimum_section].copy()
    if aligned.empty:
        raise ValueError(f"每日有效股票不足{minimum_section}只，无法稳定分层")
    available_dates = sorted(aligned["trade_date"].drop_duplicates())
    sampled_dates = available_dates[::clean_sample_interval]
    aligned = aligned.loc[aligned["trade_date"].isin(sampled_dates)].copy()
    if aligned.empty:
        raise ValueError("采样间隔过大，没有可用于分层诊断的交易日")
    aligned = aligned.sort_values(
        ["trade_date", "prediction", "instrument"],
        ascending=[True, True, False],
    )
    aligned["position"] = aligned.groupby("trade_date").cumcount()
    aligned["section_count"] = aligned.groupby("trade_date")["instrument"].transform("size")
    aligned["quantile"] = (
        np.floor(aligned["position"] * clean_quantiles / aligned["section_count"])
        .astype(int).add(1).clip(1, clean_quantiles)
    )
    daily = aligned.groupby(["trade_date", "quantile"], as_index=False).agg(
        forward_return=("forward_return", "mean"),
        mean_label=("label", "mean"),
        stock_count=("instrument", "size"),
    )
    quantile_rows = []
    for quantile, group in daily.groupby("quantile", sort=True):
        returns = group["forward_return"].to_numpy(dtype=float)
        quantile_rows.append({
            "quantile": int(quantile),
            "mean_forward_return": float(np.mean(returns)),
            "median_forward_return": float(np.median(returns)),
            "positive_day_ratio": float(np.mean(returns > 0)),
            "mean_label": float(group["mean_label"].mean()),
            "days": int(group["trade_date"].nunique()),
            "average_stock_count": float(group["stock_count"].mean()),
        })
    return_by_quantile = pd.Series(
        {item["quantile"]: item["mean_forward_return"] for item in quantile_rows},
        dtype=float,
    ).sort_index()
    aggregate_monotonicity = _safe_spearman(
        pd.Series(return_by_quantile.index, dtype=float),
        return_by_quantile.reset_index(drop=True),
    )
    adjacent = np.diff(return_by_quantile.to_numpy(dtype=float))
    adjacent_consistency = float(np.mean(adjacent >= 0)) if adjacent.size else 0.0
    pivot = daily.pivot(
        index="trade_date", columns="quantile", values="forward_return",
    )
    spread = (
        pivot[clean_quantiles] - pivot[1]
        if 1 in pivot.columns and clean_quantiles in pivot.columns
        else pd.Series(dtype=float)
    ).dropna()
    daily_monotonicity = []
    for _, group in daily.groupby("trade_date", sort=True):
        if group["quantile"].nunique() < clean_quantiles:
            continue
        value = _safe_spearman(
            group["quantile"].astype(float), group["forward_return"].astype(float),
        )
        if value is not None:
            daily_monotonicity.append(value)
    spread_mean = float(spread.mean()) if not spread.empty else 0.0
    overlap_factor = max(1.0, clean_horizon / clean_sample_interval)
    newey_west_lag = max(int(round(overlap_factor)) - 1, 0)
    spread_t_stat = _newey_west_t_stat(
        spread.to_numpy(dtype=float), lag=newey_west_lag,
    )
    effective_test_days = float(len(spread) / overlap_factor) if len(spread) else 0.0
    if effective_test_days < 10:
        spread_significance = "small_sample"
    elif spread_t_stat is None:
        spread_significance = "unavailable"
    elif spread_t_stat >= 1.96:
        spread_significance = "significant"
    elif spread_t_stat >= 1.64:
        spread_significance = "suggestive"
    elif spread_t_stat <= -1.96:
        spread_significance = "significant_negative"
    else:
        spread_significance = "insufficient"
    monotonicity = float(aggregate_monotonicity or 0.0)
    if spread_mean > 0 and monotonicity >= 0.8 and adjacent_consistency >= 0.75:
        status = "strong"
        conclusion = "预测分层具有较好的正向单调性，顶部组合未来收益明显高于底部组合。"
    elif spread_mean > 0 and monotonicity > 0:
        status = "mixed"
        conclusion = "最高分组相对最低分组收益差为正，但部分中间分层不够单调，建议继续检查跨期稳定性。"
    elif spread_mean < 0 or monotonicity < 0:
        status = "inverse"
        conclusion = "预测分层方向与预期相反，模型排序方向或当前样本外稳定性需要复核。"
    else:
        status = "weak"
        conclusion = "各预测分层收益差异较弱，暂未观察到清晰的排序经济意义。"
    if status in {"strong", "mixed"} and spread_significance == "small_sample":
        conclusion += " 当前独立有效样本不足10日，不能据此声称收益差具有统计显著性。"
    elif status in {"strong", "mixed"} and spread_significance == "insufficient":
        conclusion += " 当前收益差的Newey-West t值不足1.64，统计证据仍偏弱。"
    elif spread_significance == "suggestive":
        conclusion += " 收益差达到提示性统计证据，但尚未达到1.96的常用阈值。"
    return {
        "status": status,
        "conclusion": conclusion,
        "model_id": str(model_id),
        "model_version": int(model_version),
        "quantiles": clean_quantiles,
        "horizon_trading_days": clean_horizon,
        "sample_interval_trading_days": clean_sample_interval,
        "overlap_factor": float(overlap_factor),
        "newey_west_lag": newey_west_lag,
        "effective_test_days": effective_test_days,
        "test_rows": int(len(aligned)),
        "test_days": int(aligned["trade_date"].nunique()),
        "date_start": aligned["trade_date"].min().date(),
        "date_end": aligned["trade_date"].max().date(),
        "top_quantile_mean_return": float(return_by_quantile.get(clean_quantiles, 0.0)),
        "bottom_quantile_mean_return": float(return_by_quantile.get(1, 0.0)),
        "top_bottom_spread_mean": spread_mean,
        "top_bottom_spread_median": float(spread.median()) if not spread.empty else 0.0,
        "top_bottom_positive_ratio": float(np.mean(spread.to_numpy() > 0)) if not spread.empty else 0.0,
        "top_bottom_spread_t_stat": spread_t_stat,
        "top_bottom_spread_significance": spread_significance,
        "aggregate_monotonicity": monotonicity,
        "daily_monotonicity_mean": float(np.mean(daily_monotonicity)) if daily_monotonicity else 0.0,
        "daily_positive_monotonicity_ratio": float(np.mean(np.asarray(daily_monotonicity) > 0)) if daily_monotonicity else 0.0,
        "adjacent_consistency": adjacent_consistency,
        "groups": quantile_rows,
        "method": {
            "kind": "daily_equal_count_quantile_forward_return",
            "price": "后复权收盘价",
            "score_direction": "Q1最低分，最高分组为Q{quantiles}".format(
                quantiles=clean_quantiles,
            ),
            "minimum_stocks_per_quantile": 5,
            "pit_guard": "feature_cutoff_at不晚于信号日15:00",
            "sampling": (
                f"按交易日顺序每{clean_sample_interval}日取一个信号；"
                f"未来收益窗口重叠倍数约{overlap_factor:.2f}，"
                f"收益差t统计量使用Newey-West lag={newey_west_lag}修正"
            ),
            "disclosure": (
                f"未来{clean_horizon}日收益按采样信号计算；"
                + (
                    "相邻样本窗口不重叠；"
                    if clean_sample_interval >= clean_horizon
                    else "相邻样本窗口仍有重叠，已做自相关修正；"
                )
                + "结果不含交易成本，不等同于可成交策略回测"
            ),
        },
    }


def model_prediction_stability_diagnostics(
    *, model_id: str, model_version: int, horizon: int = 5,
    rolling_window: int = 20, quantiles: int = 5,
) -> dict[str, Any]:
    """Diagnose chronological OOS RankIC and top-bottom spread stability."""
    clean_horizon = max(1, int(horizon))
    clean_window = max(5, min(60, int(rolling_window)))
    clean_quantiles = max(2, min(10, int(quantiles)))
    predictions = _pit_safe_model_prediction_frame(
        model_id=model_id, model_version=model_version,
    )
    labels = _realized_label_frame(
        instruments=sorted(predictions["instrument"].astype(str).unique()),
        date_start=predictions["trade_date"].min().date(),
        date_end=predictions["trade_date"].max().date(),
        horizon=clean_horizon,
    )
    aligned = predictions.merge(
        labels, on=["trade_date", "instrument"], how="inner",
    ).dropna(subset=["prediction", "label", "forward_return"])
    minimum_section = clean_quantiles * 5
    section_sizes = aligned.groupby("trade_date")["instrument"].transform("size")
    aligned = aligned.loc[section_sizes >= minimum_section].copy()
    if aligned.empty:
        raise ValueError(f"每日有效股票不足{minimum_section}只，无法判断时序稳定性")

    daily_rows: list[dict[str, Any]] = []
    for trade_date, group in aligned.groupby("trade_date", sort=True):
        ordered = group.sort_values(
            ["prediction", "instrument"], ascending=[True, False],
        ).copy()
        ordered["position"] = np.arange(len(ordered), dtype=int)
        ordered["quantile"] = (
            np.floor(ordered["position"] * clean_quantiles / len(ordered))
            .astype(int).add(1).clip(1, clean_quantiles)
        )
        rank_ic = _safe_spearman(ordered["prediction"], ordered["label"])
        if rank_ic is None:
            continue
        grouped_return = ordered.groupby("quantile")["forward_return"].mean()
        spread = (
            float(grouped_return.get(clean_quantiles, np.nan)
                  - grouped_return.get(1, np.nan))
            if 1 in grouped_return.index and clean_quantiles in grouped_return.index
            else None
        )
        daily_rows.append({
            "trade_date": pd.Timestamp(trade_date),
            "rank_ic": float(rank_ic),
            "top_bottom_spread": spread,
            "stock_count": int(len(ordered)),
        })
    daily = pd.DataFrame(
        daily_rows,
        columns=["trade_date", "rank_ic", "top_bottom_spread", "stock_count"],
    ).sort_values("trade_date")
    if len(daily) < 5:
        raise ValueError("有效样本外交易日少于5日，无法判断时序稳定性")
    minimum_rolling_periods = min(clean_window, max(3, clean_window // 2))
    daily["rolling_rank_ic"] = daily["rank_ic"].rolling(
        clean_window, min_periods=minimum_rolling_periods,
    ).mean()
    daily["rolling_spread"] = daily["top_bottom_spread"].rolling(
        clean_window, min_periods=minimum_rolling_periods,
    ).mean()

    date_windows = np.array_split(
        daily["trade_date"].to_numpy(dtype="datetime64[ns]"), 3,
    )
    window_meta = [
        ("early", "测试段前期"),
        ("middle", "测试段中期"),
        ("recent", "测试段近期"),
    ]
    windows: list[dict[str, Any]] = []
    for dates_in_window, (key, label) in zip(date_windows, window_meta, strict=True):
        window = daily.loc[daily["trade_date"].isin(dates_in_window)].copy()
        rank_values = window["rank_ic"].to_numpy(dtype=float)
        spread_values = window["top_bottom_spread"].dropna().to_numpy(dtype=float)
        rank_std = float(np.std(rank_values, ddof=1)) if len(rank_values) > 1 else 0.0
        rank_mean = float(np.mean(rank_values))
        windows.append({
            "key": key,
            "label": label,
            "date_start": window["trade_date"].min().date(),
            "date_end": window["trade_date"].max().date(),
            "days": int(len(window)),
            "rows": int(window["stock_count"].sum()),
            "rank_ic_mean": rank_mean,
            "rank_ic_std": rank_std,
            "rank_ic_ir": rank_mean / rank_std if rank_std else 0.0,
            "positive_rank_ic_ratio": float(np.mean(rank_values > 0)),
            "top_bottom_spread_mean": (
                float(np.mean(spread_values)) if len(spread_values) else 0.0
            ),
            "positive_spread_ratio": (
                float(np.mean(spread_values > 0)) if len(spread_values) else 0.0
            ),
        })

    rank_values = daily["rank_ic"].to_numpy(dtype=float)
    rank_mean = float(np.mean(rank_values))
    rank_std = float(np.std(rank_values, ddof=1)) if len(rank_values) > 1 else 0.0
    overlap_factor = float(clean_horizon)
    newey_west_lag = max(clean_horizon - 1, 0)
    rank_t_stat = _newey_west_t_stat(rank_values, lag=newey_west_lag)
    effective_test_days = float(len(daily) / overlap_factor)
    if effective_test_days < 10:
        rank_significance = "small_sample"
    elif rank_t_stat is None:
        rank_significance = "unavailable"
    elif rank_t_stat >= 1.96:
        rank_significance = "significant"
    elif rank_t_stat >= 1.64:
        rank_significance = "suggestive"
    elif rank_t_stat <= -1.96:
        rank_significance = "significant_negative"
    else:
        rank_significance = "insufficient"

    early_rank_ic = float(windows[0]["rank_ic_mean"])
    recent_rank_ic = float(windows[-1]["rank_ic_mean"])
    rank_ic_change = recent_rank_ic - early_rank_ic
    recent_positive_ratio = float(windows[-1]["positive_rank_ic_ratio"])
    warnings: list[str] = []
    if effective_test_days < 10:
        warnings.append("独立有效样本不足10日，统计证据只能作为观察")
    if recent_rank_ic < 0:
        warnings.append("测试段近期平均RankIC已转为负值")
    elif recent_rank_ic < 0.02:
        warnings.append("测试段近期平均RankIC低于0.02")
    if rank_ic_change <= -0.05:
        warnings.append("近期平均RankIC较前期下降至少0.05")
    elif rank_ic_change <= -0.02:
        warnings.append("近期平均RankIC较前期出现可见衰减")
    if recent_positive_ratio < 0.50:
        warnings.append("近期正RankIC交易日不足一半")
    if rank_significance in {"insufficient", "unavailable"}:
        warnings.append("全段平均RankIC的Newey-West统计证据不足")

    if rank_mean <= 0 or recent_rank_ic < 0 or rank_ic_change <= -0.05:
        status = "unstable"
        conclusion = "样本外排序能力已出现方向反转或明显衰减，进入策略回测前应先复核数据与重训练窗口。"
    elif warnings:
        status = "mixed"
        conclusion = "样本外排序仍有正向信号，但跨期稳定性或统计证据不足，需要继续观察。"
    else:
        status = "stable"
        conclusion = "样本外RankIC在测试段内保持正向且没有明显衰减，时序稳定性相对良好。"

    series = []
    for row in daily.itertuples(index=False):
        series.append({
            "trade_date": row.trade_date.date(),
            "rank_ic": float(row.rank_ic),
            "rolling_rank_ic": (
                float(row.rolling_rank_ic) if pd.notna(row.rolling_rank_ic) else None
            ),
            "top_bottom_spread": (
                float(row.top_bottom_spread)
                if row.top_bottom_spread is not None and pd.notna(row.top_bottom_spread)
                else None
            ),
            "rolling_spread": (
                float(row.rolling_spread) if pd.notna(row.rolling_spread) else None
            ),
            "stock_count": int(row.stock_count),
        })
    return {
        "status": status,
        "conclusion": conclusion,
        "warnings": warnings,
        "model_id": str(model_id),
        "model_version": int(model_version),
        "horizon_trading_days": clean_horizon,
        "rolling_window_trading_days": clean_window,
        "quantiles": clean_quantiles,
        "date_start": daily["trade_date"].min().date(),
        "date_end": daily["trade_date"].max().date(),
        "test_days": int(len(daily)),
        "test_rows": int(daily["stock_count"].sum()),
        "effective_test_days": effective_test_days,
        "overlap_factor": overlap_factor,
        "newey_west_lag": newey_west_lag,
        "rank_ic_mean": rank_mean,
        "rank_ic_std": rank_std,
        "rank_ic_ir": rank_mean / rank_std if rank_std else 0.0,
        "positive_rank_ic_ratio": float(np.mean(rank_values > 0)),
        "rank_ic_t_stat": rank_t_stat,
        "rank_ic_significance": rank_significance,
        "early_rank_ic_mean": early_rank_ic,
        "recent_rank_ic_mean": recent_rank_ic,
        "recent_minus_early_rank_ic": rank_ic_change,
        "recent_positive_rank_ic_ratio": recent_positive_ratio,
        "windows": windows,
        "daily": series,
        "method": {
            "scope": "冻结模型PIT安全测试段的事后稳定性诊断，不重新训练或选择参数",
            "rank_ic": "每日股票截面内，预测Score与未来收益截面排名标签的Spearman相关",
            "spread": (
                f"每日等数量{clean_quantiles}组，最高分组减最低分组的未来"
                f"{clean_horizon}交易日后复权收盘收益"
            ),
            "rolling": f"{clean_window}交易日滚动均值，至少{minimum_rolling_periods}日后展示",
            "statistical_guard": (
                f"未来收益标签相邻日期约重叠{clean_horizon}倍；"
                f"RankIC均值t统计量使用Newey-West lag={newey_west_lag}修正"
            ),
            "disclosure": (
                "诊断使用已实现的未来收益，只能评价冻结模型，不能再用于当前版本调参；"
                "不含交易成本，也不等同于可成交策略回测"
            ),
        },
    }


_RELATIVE_CAP_LABELS = {
    1: "相对微盘",
    2: "相对小盘",
    3: "相对中盘",
    4: "相对大盘",
    5: "相对超大盘",
}


def _historical_market_cap_frame(
    *, instruments: list[str], date_start: date, date_end: date,
) -> pd.DataFrame:
    """Reconstruct close-date market cap with conservatively available shares."""
    price_rows = client().query(
        """
        SELECT toDate(trade_time) AS trade_date, code, toFloat64(close) AS close
        FROM starlight.ad_market_kline_daily
        WHERE code IN {codes:Array(String)}
          AND toDate(trade_time) >= {date_start:Date}
          AND toDate(trade_time) <= {date_end:Date}
          AND close IS NOT NULL AND close > 0
        ORDER BY trade_date, code
        """,
        parameters={
            "codes": instruments,
            "date_start": date_start,
            "date_end": date_end,
        },
    ).result_rows
    share_rows = client().query(
        """
        SELECT market_code, ann_date, change_date, toFloat64(tot_share)
        FROM starlight.ad_equity_structure
        WHERE market_code IN {codes:Array(String)}
          AND is_valid = 1
          AND tot_share IS NOT NULL AND tot_share > 0
          AND (ann_date <= {date_end:Date} OR change_date <= {date_end:Date})
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
    empty = pd.DataFrame(columns=[
        "trade_date", "instrument", "market_cap", "log_market_cap",
        "equity_available_date",
    ])
    if prices.empty or shares.empty:
        return empty
    prices["trade_date"] = pd.to_datetime(prices["trade_date"], errors="coerce")
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    for column in ["ann_date", "change_date"]:
        shares[column] = pd.to_datetime(shares[column], errors="coerce")
    shares["total_share_10k"] = pd.to_numeric(
        shares["total_share_10k"], errors="coerce",
    )
    shares["equity_available_date"] = shares[
        ["ann_date", "change_date"]
    ].max(axis=1)
    shares = shares.dropna(subset=["equity_available_date", "total_share_10k"])
    shares = shares.loc[
        (shares["total_share_10k"] > 0)
        & (shares["equity_available_date"] <= pd.Timestamp(date_end))
    ]
    prices = prices.dropna(subset=["trade_date", "close"])
    prices = prices.loc[prices["close"] > 0]
    if prices.empty or shares.empty:
        return empty

    merged_parts: list[pd.DataFrame] = []
    share_groups = {
        str(instrument): group.sort_values("equity_available_date")
        for instrument, group in shares.groupby("instrument", sort=False)
    }
    for instrument, price_group in prices.groupby("instrument", sort=False):
        share_group = share_groups.get(str(instrument))
        if share_group is None or share_group.empty:
            continue
        merged = pd.merge_asof(
            price_group.sort_values("trade_date"),
            share_group[["equity_available_date", "total_share_10k"]],
            left_on="trade_date",
            right_on="equity_available_date",
            direction="backward",
        )
        merged_parts.append(merged)
    if not merged_parts:
        return empty
    result = pd.concat(merged_parts, ignore_index=True).dropna(
        subset=["total_share_10k"],
    )
    result["market_cap"] = (
        result["close"] * result["total_share_10k"] * 10_000.0
    )
    result = result.loc[
        np.isfinite(result["market_cap"]) & (result["market_cap"] > 0)
    ].copy()
    result["log_market_cap"] = np.log(result["market_cap"])
    return result[[
        "trade_date", "instrument", "market_cap", "log_market_cap",
        "equity_available_date",
    ]]


def _historical_industry_mapping(
    *, observations: pd.DataFrame, date_start: date, date_end: date,
) -> pd.DataFrame:
    """Map observations to historical Shenwan L1 effective intervals."""
    empty = pd.DataFrame(columns=["trade_date", "instrument", "industry"])
    if observations.empty:
        return empty
    instruments = sorted(observations["instrument"].astype(str).unique())
    rows = client().query(
        """
        SELECT c.con_code, c.in_date, c.out_date, b.level1_name
        FROM starlight.ad_industry_constituent c
        INNER JOIN starlight.ad_industry_base_info b
          ON c.index_code = b.index_code
        WHERE c.con_code IN {codes:Array(String)}
          AND toString(b.level_type) = '1'
          AND c.in_date <= {date_end:Date}
          AND (c.out_date IS NULL OR c.out_date >= {date_start:Date})
          AND b.level1_name IS NOT NULL AND b.level1_name != ''
        ORDER BY c.con_code, c.in_date, c.out_date
        """,
        parameters={
            "codes": instruments,
            "date_start": date_start,
            "date_end": date_end,
        },
    ).result_rows
    intervals = pd.DataFrame(
        rows, columns=["instrument", "in_date", "out_date", "industry"],
    )
    if intervals.empty:
        return empty
    intervals["in_date"] = pd.to_datetime(intervals["in_date"], errors="coerce")
    intervals["out_date"] = pd.to_datetime(intervals["out_date"], errors="coerce")
    intervals = intervals.dropna(subset=["in_date", "industry"])
    observed = observations[["trade_date", "instrument"]].drop_duplicates().copy()
    observed["trade_date"] = pd.to_datetime(observed["trade_date"], errors="coerce")
    candidates = observed.merge(intervals, on="instrument", how="inner")
    candidates = candidates.loc[
        (candidates["trade_date"] >= candidates["in_date"])
        & (
            candidates["out_date"].isna()
            | (candidates["trade_date"] <= candidates["out_date"])
        )
    ]
    if candidates.empty:
        return empty
    candidates = candidates.sort_values(
        ["trade_date", "instrument", "in_date", "industry"],
    ).drop_duplicates(["trade_date", "instrument"], keep="last")
    return candidates[["trade_date", "instrument", "industry"]]


def _assign_daily_equal_count_bucket(
    frame: pd.DataFrame, *, value_column: str, bucket_column: str, buckets: int,
) -> pd.DataFrame:
    ordered = frame.sort_values(
        ["trade_date", value_column, "instrument"],
        ascending=[True, True, False],
    ).copy()
    ordered["_bucket_position"] = ordered.groupby("trade_date").cumcount()
    ordered["_bucket_section_count"] = ordered.groupby("trade_date")[
        "instrument"
    ].transform("size")
    ordered[bucket_column] = (
        np.floor(
            ordered["_bucket_position"] * buckets
            / ordered["_bucket_section_count"]
        ).astype(int).add(1).clip(1, buckets)
    )
    return ordered.drop(columns=["_bucket_position", "_bucket_section_count"])


def model_prediction_exposure_diagnostics(
    *, model_id: str, model_version: int, horizon: int = 5,
    score_quantiles: int = 5,
) -> dict[str, Any]:
    """Disclose size and industry exposures of PIT-safe OOS score buckets."""
    clean_quantiles = max(3, min(10, int(score_quantiles)))
    clean_horizon = max(1, int(horizon))
    predictions = _pit_safe_model_prediction_frame(
        model_id=model_id, model_version=model_version,
    )
    instruments = sorted(predictions["instrument"].astype(str).unique())
    date_start = predictions["trade_date"].min().date()
    date_end = predictions["trade_date"].max().date()
    labels = _realized_label_frame(
        instruments=instruments,
        date_start=date_start,
        date_end=date_end,
        horizon=clean_horizon,
    )
    aligned = predictions.merge(
        labels, on=["trade_date", "instrument"], how="inner",
    ).dropna(subset=["prediction", "forward_return"])
    minimum_section = clean_quantiles * 5
    section_sizes = aligned.groupby("trade_date")["instrument"].transform("size")
    aligned = aligned.loc[section_sizes >= minimum_section].copy()
    if aligned.empty:
        raise ValueError(f"每日有效股票不足{minimum_section}只，无法诊断风格暴露")
    aligned = _assign_daily_equal_count_bucket(
        aligned,
        value_column="prediction",
        bucket_column="score_quantile",
        buckets=clean_quantiles,
    )

    market_caps = _historical_market_cap_frame(
        instruments=instruments, date_start=date_start, date_end=date_end,
    )
    cap_aligned = aligned.merge(
        market_caps, on=["trade_date", "instrument"], how="inner",
    ).dropna(subset=["market_cap", "log_market_cap"])
    cap_coverage = float(len(cap_aligned) / len(aligned)) if len(aligned) else 0.0
    cap_aligned = _assign_daily_equal_count_bucket(
        cap_aligned,
        value_column="market_cap",
        bucket_column="cap_bucket",
        buckets=5,
    ) if not cap_aligned.empty else cap_aligned.assign(cap_bucket=pd.Series(dtype=int))

    cap_correlations: list[float] = []
    for _, group in cap_aligned.groupby("trade_date", sort=True):
        value = _safe_spearman(group["prediction"], group["log_market_cap"])
        if value is not None:
            cap_correlations.append(value)
    mean_cap_correlation = float(np.mean(cap_correlations)) if cap_correlations else 0.0

    cap_daily = cap_aligned.groupby(
        ["trade_date", "score_quantile", "cap_bucket"], as_index=False,
    ).agg(
        forward_return=("forward_return", "mean"),
        stock_count=("instrument", "size"),
    )
    cap_matrix: list[dict[str, Any]] = []
    for score_quantile in range(1, clean_quantiles + 1):
        for cap_bucket in range(1, 6):
            group = cap_daily.loc[
                (cap_daily["score_quantile"] == score_quantile)
                & (cap_daily["cap_bucket"] == cap_bucket)
            ]
            returns = group["forward_return"].to_numpy(dtype=float)
            cap_matrix.append({
                "score_quantile": score_quantile,
                "cap_bucket": cap_bucket,
                "cap_label": _RELATIVE_CAP_LABELS[cap_bucket],
                "sample_rows": int(group["stock_count"].sum()) if not group.empty else 0,
                "days": int(group["trade_date"].nunique()) if not group.empty else 0,
                "average_stock_count": float(group["stock_count"].mean()) if not group.empty else 0.0,
                "mean_forward_return": float(np.mean(returns)) if returns.size else None,
                "positive_day_ratio": float(np.mean(returns > 0)) if returns.size else None,
            })

    cap_days = sorted(cap_aligned["trade_date"].drop_duplicates())
    cap_grid = pd.MultiIndex.from_product(
        [cap_days, range(1, 6)], names=["trade_date", "cap_bucket"],
    ).to_frame(index=False)
    universe_cap = cap_aligned.groupby(
        ["trade_date", "cap_bucket"], as_index=False,
    ).agg(stock_count=("instrument", "size"), mean_market_cap=("market_cap", "mean"))
    universe_totals = cap_aligned.groupby("trade_date")["instrument"].size().rename(
        "universe_count",
    )
    universe_cap = cap_grid.merge(
        universe_cap, on=["trade_date", "cap_bucket"], how="left",
    ).merge(universe_totals, on="trade_date", how="left")
    universe_cap["universe_weight"] = (
        universe_cap["stock_count"].fillna(0)
        / universe_cap["universe_count"].replace(0, np.nan)
    ).fillna(0.0)
    top_cap_rows = cap_aligned.loc[
        cap_aligned["score_quantile"] == clean_quantiles
    ]
    top_cap = top_cap_rows.groupby(
        ["trade_date", "cap_bucket"], as_index=False,
    ).agg(
        stock_count=("instrument", "size"),
        forward_return=("forward_return", "mean"),
    )
    top_totals = top_cap_rows.groupby("trade_date")["instrument"].size().rename(
        "top_count",
    )
    top_cap = cap_grid.merge(
        top_cap, on=["trade_date", "cap_bucket"], how="left",
    ).merge(top_totals, on="trade_date", how="left")
    top_cap["top_weight"] = (
        top_cap["stock_count"].fillna(0)
        / top_cap["top_count"].replace(0, np.nan)
    ).fillna(0.0)
    cap_exposure: list[dict[str, Any]] = []
    for cap_bucket in range(1, 6):
        universe_group = universe_cap.loc[universe_cap["cap_bucket"] == cap_bucket]
        top_group = top_cap.loc[top_cap["cap_bucket"] == cap_bucket]
        universe_weight = float(universe_group["universe_weight"].mean()) if not universe_group.empty else 0.0
        top_weight = float(top_group["top_weight"].mean()) if not top_group.empty else 0.0
        cap_exposure.append({
            "cap_bucket": cap_bucket,
            "cap_label": _RELATIVE_CAP_LABELS[cap_bucket],
            "universe_weight": universe_weight,
            "top_quantile_weight": top_weight,
            "active_weight": top_weight - universe_weight,
            "top_mean_forward_return": float(top_group["forward_return"].mean()) if top_group["forward_return"].notna().any() else None,
            "mean_market_cap": float(universe_group["mean_market_cap"].mean()) if universe_group["mean_market_cap"].notna().any() else None,
        })

    industry_mapping = _historical_industry_mapping(
        observations=aligned,
        date_start=date_start,
        date_end=date_end,
    )
    industry_aligned = aligned.merge(
        industry_mapping, on=["trade_date", "instrument"], how="inner",
    ).dropna(subset=["industry"])
    industry_coverage = (
        float(len(industry_aligned) / len(aligned)) if len(aligned) else 0.0
    )
    industry_rows: list[dict[str, Any]] = []
    top_industry_hhi = 0.0
    universe_industry_hhi = 0.0
    if not industry_aligned.empty:
        industry_days = sorted(industry_aligned["trade_date"].drop_duplicates())
        industries = sorted(industry_aligned["industry"].astype(str).unique())
        industry_grid = pd.MultiIndex.from_product(
            [industry_days, industries], names=["trade_date", "industry"],
        ).to_frame(index=False)
        universe_industry = industry_aligned.groupby(
            ["trade_date", "industry"], as_index=False,
        ).agg(stock_count=("instrument", "size"))
        universe_industry_total = industry_aligned.groupby("trade_date")[
            "instrument"
        ].size().rename("universe_count")
        universe_industry = industry_grid.merge(
            universe_industry, on=["trade_date", "industry"], how="left",
        ).merge(universe_industry_total, on="trade_date", how="left")
        universe_industry["universe_weight"] = (
            universe_industry["stock_count"].fillna(0)
            / universe_industry["universe_count"].replace(0, np.nan)
        ).fillna(0.0)
        top_industry_source = industry_aligned.loc[
            industry_aligned["score_quantile"] == clean_quantiles
        ]
        top_industry = top_industry_source.groupby(
            ["trade_date", "industry"], as_index=False,
        ).agg(
            stock_count=("instrument", "size"),
            forward_return=("forward_return", "mean"),
        )
        top_industry_total = top_industry_source.groupby("trade_date")[
            "instrument"
        ].size().rename("top_count")
        top_industry = industry_grid.merge(
            top_industry, on=["trade_date", "industry"], how="left",
        ).merge(top_industry_total, on="trade_date", how="left")
        top_industry["top_weight"] = (
            top_industry["stock_count"].fillna(0)
            / top_industry["top_count"].replace(0, np.nan)
        ).fillna(0.0)
        top_industry_hhi = float(
            top_industry.groupby("trade_date")["top_weight"].apply(
                lambda values: float(np.square(values).sum()),
            ).mean()
        )
        universe_industry_hhi = float(
            universe_industry.groupby("trade_date")["universe_weight"].apply(
                lambda values: float(np.square(values).sum()),
            ).mean()
        )
        for industry in industries:
            universe_group = universe_industry.loc[
                universe_industry["industry"] == industry
            ]
            top_group = top_industry.loc[top_industry["industry"] == industry]
            universe_weight = float(universe_group["universe_weight"].mean())
            top_weight = float(top_group["top_weight"].mean())
            industry_rows.append({
                "industry": industry,
                "universe_weight": universe_weight,
                "top_quantile_weight": top_weight,
                "active_weight": top_weight - universe_weight,
                "top_mean_forward_return": float(top_group["forward_return"].mean()) if top_group["forward_return"].notna().any() else None,
                "top_sample_rows": int(top_group["stock_count"].fillna(0).sum()),
            })
        industry_rows.sort(key=lambda item: abs(item["active_weight"]), reverse=True)

    max_industry_weight = max(
        (float(row["top_quantile_weight"]) for row in industry_rows), default=0.0,
    )
    max_industry_active = max(
        (abs(float(row["active_weight"])) for row in industry_rows), default=0.0,
    )
    warnings: list[str] = []
    if cap_coverage < 0.8:
        warnings.append("市值映射覆盖率低于80%，市值暴露结论只可作为有限样本参考")
    if industry_coverage < 0.8:
        warnings.append("行业映射覆盖率低于80%，行业集中度结论只可作为有限样本参考")
    if abs(mean_cap_correlation) >= 0.15:
        direction = "大盘" if mean_cap_correlation > 0 else "小盘"
        warnings.append(f"模型分数与对数市值相关性较高，存在明显{direction}风格倾向")
    if max_industry_active >= 0.08:
        warnings.append("最高分组相对股票池存在超过8%的行业主动暴露")
    if cap_coverage < 0.8 or industry_coverage < 0.8:
        status = "limited"
        conclusion = "部分暴露数据覆盖不足；当前结果可用于排查风格来源，但不应据此直接修改模型。"
    elif warnings:
        status = "warning"
        conclusion = "模型最高分组存在可见的市值或行业集中，策略回测前应设置相应风险约束。"
    else:
        status = "stable"
        conclusion = "未观察到明显的市值或单一行业集中，模型排序暴露相对可控。"
    return {
        "status": status,
        "conclusion": conclusion,
        "warnings": warnings,
        "model_id": str(model_id),
        "model_version": int(model_version),
        "score_quantiles": clean_quantiles,
        "top_quantile": clean_quantiles,
        "horizon_trading_days": clean_horizon,
        "test_rows": int(len(aligned)),
        "test_days": int(aligned["trade_date"].nunique()),
        "date_start": aligned["trade_date"].min().date(),
        "date_end": aligned["trade_date"].max().date(),
        "market_cap": {
            "coverage_ratio": cap_coverage,
            "mapped_rows": int(len(cap_aligned)),
            "mean_daily_score_log_cap_spearman": mean_cap_correlation,
            "matrix": cap_matrix,
            "exposure": cap_exposure,
        },
        "industry": {
            "coverage_ratio": industry_coverage,
            "mapped_rows": int(len(industry_aligned)),
            "top_quantile_hhi": top_industry_hhi,
            "universe_hhi": universe_industry_hhi,
            "max_top_quantile_weight": max_industry_weight,
            "max_absolute_active_weight": max_industry_active,
            "exposure": industry_rows,
        },
        "method": {
            "scope": "PIT安全样本外预测的暴露诊断，不是策略回测",
            "score_bucket": f"每日等数量{clean_quantiles}组，Q1最低分，Q{clean_quantiles}最高分",
            "cap_bucket": "每日股票池内市值等数量5组，为相对市值档而非全市场固定阈值",
            "cap_pit_guard": (
                "总市值=信号日未复权收盘价×当时最新总股本×10000；"
                "股本记录必须同时满足公告日和变更日不晚于信号日"
            ),
            "industry_pit_level": "historical_effective_interval_without_publication_timestamp",
            "industry_disclosure": (
                "申万一级行业仅按历史纳入/剔除生效区间还原；源表没有独立公告可用时间，"
                "不满足完整PIT可用性，只用于暴露披露，不进入训练或回测"
            ),
            "return_disclosure": (
                f"单元格收益为未来{clean_horizon}交易日后复权收盘收益，"
                "先做每日等权再跨日平均；窗口重叠且不含交易成本"
            ),
        },
    }


def _pit_safe_raw_prediction_frame(
    *, model_id: str, model_version: int,
) -> pd.DataFrame:
    database = settings().model_database
    rows = client().query(
        f"""
        WITH latest AS (
            SELECT trade_date, entity_code,
                   argMax(raw_prediction,
                          tuple(updated_at, computed_at, inference_run_id))
                       AS raw_prediction,
                   argMax(feature_cutoff_at,
                          tuple(updated_at, computed_at, inference_run_id))
                       AS feature_cutoff_at
            FROM {database}.model_predictions_daily FINAL
            WHERE model_id = {{model_id:String}}
              AND model_version = {{model_version:UInt32}}
            GROUP BY trade_date, entity_code
        )
        SELECT trade_date, entity_code, raw_prediction
        FROM latest
        WHERE feature_cutoff_at <=
              toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR
        ORDER BY trade_date, entity_code
        """,
        parameters={"model_id": model_id, "model_version": int(model_version)},
    ).result_rows
    frame = pd.DataFrame(
        rows, columns=["trade_date", "instrument", "raw_prediction"],
    )
    if frame.empty:
        raise ValueError("模型没有PIT安全的原始样本外预测")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["raw_prediction"] = pd.to_numeric(
        frame["raw_prediction"], errors="coerce",
    )
    frame = frame.dropna(subset=["trade_date", "raw_prediction"])
    if frame.empty:
        raise ValueError("模型原始样本外预测全部为空")
    return frame


def _distribution_psi(
    baseline: np.ndarray, current: np.ndarray, *, bins: int,
) -> float:
    if not baseline.size or not current.size:
        return 0.0
    quantiles = np.linspace(0.0, 1.0, max(3, int(bins)) + 1)
    raw_edges = np.quantile(baseline, quantiles)
    inner = np.unique(raw_edges[1:-1])
    if inner.size < 2:
        combined = np.concatenate([baseline, current])
        minimum = float(np.min(combined))
        maximum = float(np.max(combined))
        if maximum <= minimum:
            return 0.0
        inner = np.linspace(minimum, maximum, max(3, int(bins)) + 1)[1:-1]
    edges = np.concatenate(([-np.inf], inner, [np.inf]))
    baseline_counts, _ = np.histogram(baseline, bins=edges)
    current_counts, _ = np.histogram(current, bins=edges)
    epsilon = 1e-6
    baseline_ratio = np.clip(baseline_counts / baseline_counts.sum(), epsilon, None)
    current_ratio = np.clip(current_counts / current_counts.sum(), epsilon, None)
    return float(np.sum(
        (current_ratio - baseline_ratio) * np.log(current_ratio / baseline_ratio),
    ))


def _raw_prediction_window_summary(
    frame: pd.DataFrame, *, key: str, label: str,
) -> dict[str, Any]:
    values = frame["raw_prediction"].to_numpy(dtype=float)
    daily = frame.groupby("trade_date")["raw_prediction"].agg(
        ["std", lambda series: series.quantile(0.90) - series.quantile(0.10)],
    )
    daily.columns = ["std", "p90_p10"]
    return {
        "key": key,
        "label": label,
        "date_start": frame["trade_date"].min().date(),
        "date_end": frame["trade_date"].max().date(),
        "rows": int(len(frame)),
        "days": int(frame["trade_date"].nunique()),
        "mean": float(np.mean(values)),
        "std": float(np.std(values)),
        "p05": float(np.quantile(values, 0.05)),
        "p50": float(np.quantile(values, 0.50)),
        "p95": float(np.quantile(values, 0.95)),
        "mean_daily_std": float(daily["std"].dropna().mean()) if daily["std"].notna().any() else 0.0,
        "mean_daily_p90_p10": float(daily["p90_p10"].mean()),
    }


def model_prediction_distribution_diagnostics(
    *, model_id: str, model_version: int, bins: int = 10,
) -> dict[str, Any]:
    """Detect drift or collapse in OOS raw prediction distributions."""
    clean_bins = max(5, min(30, int(bins)))
    frame = _pit_safe_raw_prediction_frame(
        model_id=model_id, model_version=model_version,
    )
    dates = sorted(frame["trade_date"].drop_duplicates())
    if len(dates) < 9:
        raise ValueError("原始预测少于9个交易日，无法稳定比较跨期分布")
    date_windows = np.array_split(np.asarray(dates, dtype="datetime64[ns]"), 3)
    window_meta = [
        ("early", "测试段前期"),
        ("middle", "测试段中期"),
        ("recent", "测试段近期"),
    ]
    windows: list[dict[str, Any]] = []
    window_frames: list[pd.DataFrame] = []
    for dates_in_window, (key, label) in zip(date_windows, window_meta, strict=True):
        window_frame = frame.loc[frame["trade_date"].isin(dates_in_window)].copy()
        window_frames.append(window_frame)
        windows.append(_raw_prediction_window_summary(
            window_frame, key=key, label=label,
        ))
    baseline_values = window_frames[0]["raw_prediction"].to_numpy(dtype=float)
    for index, window_frame in enumerate(window_frames):
        windows[index]["psi_vs_early"] = (
            0.0 if index == 0 else _distribution_psi(
                baseline_values,
                window_frame["raw_prediction"].to_numpy(dtype=float),
                bins=clean_bins,
            )
        )

    recent_values = window_frames[-1]["raw_prediction"].to_numpy(dtype=float)
    baseline_std = float(np.std(baseline_values))
    recent_std = float(np.std(recent_values))
    dispersion_ratio = recent_std / baseline_std if baseline_std > 1e-12 else 1.0
    standardized_median_shift = (
        float(abs(np.median(recent_values) - np.median(baseline_values)) / baseline_std)
        if baseline_std > 1e-12 else 0.0
    )
    latest_psi = float(windows[-1]["psi_vs_early"])

    visual_values = np.concatenate([baseline_values, recent_values])
    lower = float(np.quantile(visual_values, 0.01))
    upper = float(np.quantile(visual_values, 0.99))
    if upper <= lower:
        lower -= 0.5
        upper += 0.5
    visual_edges = np.linspace(lower, upper, clean_bins + 1)
    histogram: list[dict[str, Any]] = []
    for key, values in [("early", baseline_values), ("recent", recent_values)]:
        clipped = np.clip(values, lower, upper)
        counts, _ = np.histogram(clipped, bins=visual_edges)
        for index, count in enumerate(counts):
            histogram.append({
                "window": key,
                "bin": index + 1,
                "lower": float(visual_edges[index]),
                "upper": float(visual_edges[index + 1]),
                "count": int(count),
                "ratio": float(count / len(values)) if len(values) else 0.0,
            })

    daily_rows: list[dict[str, Any]] = []
    for trade_date, group in frame.groupby("trade_date", sort=True):
        values = group["raw_prediction"].to_numpy(dtype=float)
        daily_rows.append({
            "trade_date": trade_date.date(),
            "mean": float(np.mean(values)),
            "std": float(np.std(values)),
            "p10": float(np.quantile(values, 0.10)),
            "p50": float(np.quantile(values, 0.50)),
            "p90": float(np.quantile(values, 0.90)),
            "rows": int(len(values)),
        })

    warnings: list[str] = []
    if latest_psi >= 0.25:
        warnings.append("近期原始预测相对测试段前期出现显著分布漂移（PSI≥0.25）")
    elif latest_psi >= 0.10:
        warnings.append("近期原始预测相对测试段前期出现中等分布漂移（PSI≥0.10）")
    if dispersion_ratio < 0.5:
        warnings.append("近期原始预测离散度不足前期一半，存在输出塌缩风险")
    elif dispersion_ratio > 2.0:
        warnings.append("近期原始预测离散度超过前期两倍，输出波动明显放大")
    if standardized_median_shift >= 1.0:
        warnings.append("近期预测中位数相对前期偏移超过一个前期标准差")
    if latest_psi >= 0.25 or dispersion_ratio < 0.5 or dispersion_ratio > 2.0:
        status = "severe"
        conclusion = "原始预测输出出现显著漂移或离散度异常，需要结合特征漂移和数据批次排查。"
    elif warnings:
        status = "warning"
        conclusion = "原始预测分布已有可见变化，建议观察后续推理截面是否继续偏移。"
    else:
        status = "stable"
        conclusion = "原始预测的中心与离散度在独立测试段内保持相对稳定。"
    return {
        "status": status,
        "conclusion": conclusion,
        "warnings": warnings,
        "model_id": str(model_id),
        "model_version": int(model_version),
        "date_start": frame["trade_date"].min().date(),
        "date_end": frame["trade_date"].max().date(),
        "rows": int(len(frame)),
        "days": int(frame["trade_date"].nunique()),
        "bins": clean_bins,
        "latest_psi_vs_early": latest_psi,
        "recent_to_early_std_ratio": float(dispersion_ratio),
        "standardized_median_shift": standardized_median_shift,
        "windows": windows,
        "histogram": histogram,
        "daily": daily_rows,
        "method": {
            "source": "PIT安全样本外raw_prediction；同股票同日只取最新推理批次",
            "split": "按交易日顺序将独立测试预测等分为前期、中期、近期",
            "why_raw_prediction": (
                "统一Score是每日截面排名映射到[-1,1]，天然接近均匀分布；"
                "分布健康度必须检查模型原始预测，不能用Score掩盖输出塌缩"
            ),
            "guard": "只做预测输出诊断，不读取未来收益，不重新训练或选择模型参数",
        },
    }


def ensemble_model_diagnostics(
    *, sources: list[Mapping[str, Any]], horizon: int = 5,
) -> dict[str, Any]:
    """Diagnose an immutable ensemble on its common PIT-safe OOS predictions."""
    condition, parameters = _ensemble_source_condition(sources)
    database = settings().model_database
    rows = client().query(
        f"""
        SELECT model_id, model_version, trade_date, entity_code,
               argMax(score, tuple(updated_at, computed_at, inference_run_id))
                   AS source_score
        FROM {database}.model_predictions_daily FINAL
        WHERE ({condition})
          AND feature_cutoff_at <=
              toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR
        GROUP BY model_id, model_version, trade_date, entity_code
        ORDER BY trade_date, entity_code, model_id, model_version
        """,
        parameters=parameters,
    ).result_rows
    if not rows:
        raise ValueError("源模型没有可诊断的共同样本外预测")
    long = pd.DataFrame(
        rows,
        columns=["model_id", "model_version", "trade_date", "instrument", "score"],
    )
    long["trade_date"] = pd.to_datetime(long["trade_date"])
    long["score"] = pd.to_numeric(long["score"], errors="coerce")
    long["source_key"] = long.apply(
        lambda row: _source_key(str(row["model_id"]), int(row["model_version"])),
        axis=1,
    )
    source_views = [_diagnostic_source_view(source) for source in sources]
    source_keys = [str(item["source_key"]) for item in source_views]
    pivot = long.pivot_table(
        index=["trade_date", "instrument"],
        columns="source_key",
        values="score",
        aggfunc="last",
    )
    missing_keys = [key for key in source_keys if key not in pivot.columns]
    if missing_keys:
        raise ValueError("部分源模型没有可诊断预测")
    common = pivot[source_keys].dropna().reset_index()
    if common.empty:
        raise ValueError("源模型没有完整重叠的诊断样本")
    instruments = sorted(common["instrument"].astype(str).unique())
    labels = _realized_label_frame(
        instruments=instruments,
        date_start=common["trade_date"].min().date(),
        date_end=common["trade_date"].max().date(),
        horizon=horizon,
    )
    aligned = common.merge(
        labels, on=["trade_date", "instrument"], how="inner",
    ).dropna(subset=[*source_keys, "label"])
    if aligned.empty:
        raise ValueError("源模型共同预测无法与未来收益标签对齐")

    source_weights = np.asarray(
        [float(item.get("weight") or 0.0) for item in source_views], dtype=float,
    )
    if not np.isfinite(source_weights).all() or float(source_weights.sum()) <= 0:
        raise ValueError("融合模型权重无效")
    source_weights = source_weights / source_weights.sum()
    for item, weight in zip(source_views, source_weights, strict=True):
        item["weight"] = float(weight)

    pairwise = np.eye(len(source_keys), dtype=float)
    pair_values: list[float] = []
    for left in range(len(source_keys)):
        for right in range(left + 1, len(source_keys)):
            correlations: list[float] = []
            for _, group in aligned.groupby("trade_date", sort=True):
                if (
                    group[source_keys[left]].nunique() <= 1
                    or group[source_keys[right]].nunique() <= 1
                ):
                    continue
                value = group[source_keys[left]].corr(
                    group[source_keys[right]], method="spearman",
                )
                if pd.notna(value):
                    correlations.append(float(value))
            mean_value = float(np.mean(correlations)) if correlations else 0.0
            pairwise[left, right] = mean_value
            pairwise[right, left] = mean_value
            pair_values.append(mean_value)

    baseline_score = _rank_ensemble_score(aligned, source_keys, source_weights)
    baseline = _daily_rank_metrics(aligned, baseline_score)
    source_metrics = {
        key: _daily_rank_metrics(
            aligned,
            _rank_ensemble_score(aligned, [key], np.asarray([1.0])),
        )
        for key in source_keys
    }
    marginal: list[dict[str, Any]] = []
    for index, source in enumerate(source_views):
        if len(source_keys) == 2:
            remaining_score = _rank_ensemble_score(
                aligned, [source_keys[1 - index]], np.asarray([1.0]),
            )
        else:
            keep = [position for position in range(len(source_keys)) if position != index]
            remaining_weights = source_weights[keep]
            remaining_weights = remaining_weights / remaining_weights.sum()
            remaining_score = _rank_ensemble_score(
                aligned,
                [source_keys[position] for position in keep],
                remaining_weights,
            )
        without = _daily_rank_metrics(aligned, remaining_score)
        marginal.append({
            **source,
            "standalone_rank_ic": source_metrics[str(source["source_key"])]["rank_ic"],
            "standalone_ic_ir": source_metrics[str(source["source_key"])]["ic_ir"],
            "without_rank_ic": without["rank_ic"],
            "without_ic_ir": without["ic_ir"],
            "marginal_rank_ic": baseline["rank_ic"] - without["rank_ic"],
        })

    sensitivity_weights = _ensemble_sensitivity_weights(source_weights)
    sensitivity: list[dict[str, Any]] = []
    for scenario_index, weights in enumerate(sensitivity_weights):
        score = _rank_ensemble_score(aligned, source_keys, weights)
        metrics = _daily_rank_metrics(aligned, score)
        sensitivity.append({
            "scenario": scenario_index + 1,
            "is_baseline": bool(np.allclose(weights, source_weights)),
            "weights": [
                {
                    "source_key": source_keys[index],
                    "model_id": source_views[index]["model_id"],
                    "model_version": source_views[index]["model_version"],
                    "weight": float(weight),
                }
                for index, weight in enumerate(weights)
            ],
            "rank_ic": metrics["rank_ic"],
            "ic_ir": metrics["ic_ir"],
            "delta_rank_ic": metrics["rank_ic"] - baseline["rank_ic"],
        })

    average_correlation = float(np.mean(pair_values)) if pair_values else 0.0
    warnings: list[str] = []
    if average_correlation >= 0.8:
        warnings.append("源模型平均相关性较高，融合后的分散化收益可能有限")
    negative_sources = [
        str(item.get("name") or item.get("model_id"))
        for item in marginal if float(item["marginal_rank_ic"]) < 0
    ]
    if negative_sources:
        warnings.append(f"剔除后RankIC反而提高：{'、'.join(negative_sources)}")
    time_stability = _ensemble_time_stability(
        aligned=aligned,
        sources=source_views,
        source_keys=source_keys,
        source_weights=source_weights,
    )
    return {
        "evaluation_scope": "common_pit_safe_oos",
        "usage": "diagnostic_only_not_for_weight_selection",
        "label_horizon_trading_days": max(1, int(horizon)),
        "test_rows": int(len(aligned)),
        "test_days": int(aligned["trade_date"].nunique()),
        "date_start": aligned["trade_date"].min().date(),
        "date_end": aligned["trade_date"].max().date(),
        "sources": source_views,
        "correlation_matrix": pairwise.tolist(),
        "average_pairwise_correlation": average_correlation,
        "diversity_score": max(0.0, min(1.0, 1.0 - abs(average_correlation))),
        "baseline": baseline,
        "marginal_contributions": marginal,
        "weight_sensitivity": sensitivity,
        "time_stability": time_stability,
        "warnings": warnings,
    }


def _realized_label_frame(
    *, instruments: list[str], date_start: date, date_end: date, horizon: int,
) -> pd.DataFrame:
    price_rows = client().query(
        """
        SELECT toDate(k.trade_time) AS trade_date, k.code,
               k.close * ifNull(a.backward_adj_factor, 1.0) AS adjusted_close
        FROM starlight.ad_market_kline_daily k
        ASOF LEFT JOIN (
            SELECT code AS adjustment_code, toDate(divid_operate_date) AS factor_date,
                   toFloat64OrNull(nullIf(back_adjust_factor, '')) AS backward_adj_factor
            FROM baostock.bs_adjust_factor
            WHERE code IN {codes:Array(String)}
            ORDER BY code, factor_date
        ) a ON k.code = a.adjustment_code
           AND toDate(k.trade_time) >= a.factor_date
        WHERE k.code IN {codes:Array(String)}
          AND toDate(k.trade_time) >= {date_start:Date}
          AND toDate(k.trade_time) <= {date_end:Date}
              + toIntervalDay({calendar_buffer:UInt32})
          AND k.close IS NOT NULL AND k.close > 0
        ORDER BY trade_date, code
        """,
        parameters={
            "codes": instruments, "date_start": date_start, "date_end": date_end,
            "calendar_buffer": max(30, max(1, int(horizon)) * 3 + 10),
        },
    ).result_rows
    prices = pd.DataFrame(
        price_rows, columns=["trade_date", "instrument", "adjusted_close"],
    )
    if prices.empty:
        raise ValueError("模型评价缺少后复权收盘价")
    prices["trade_date"] = pd.to_datetime(prices["trade_date"])
    prices["adjusted_close"] = pd.to_numeric(
        prices["adjusted_close"], errors="coerce",
    )
    pivot = prices.drop_duplicates(
        ["trade_date", "instrument"], keep="last",
    ).pivot(
        index="trade_date", columns="instrument", values="adjusted_close",
    ).sort_index()
    future_return = pivot.shift(-max(1, int(horizon))).div(pivot).sub(1.0)
    percentile = future_return.rank(axis=1, pct=True, method="average")
    labels = (2.0 * percentile - 1.0).stack(future_stack=True).dropna().rename(
        "label",
    ).reset_index()
    returns = future_return.stack(future_stack=True).dropna().rename(
        "forward_return",
    ).reset_index()
    return labels.merge(returns, on=["trade_date", "instrument"], how="inner")


def _safe_spearman(left: pd.Series, right: pd.Series) -> float | None:
    if len(left) < 2 or left.nunique() <= 1 or right.nunique() <= 1:
        return None
    value = left.reset_index(drop=True).corr(
        right.reset_index(drop=True), method="spearman",
    )
    return float(value) if pd.notna(value) else None


def _newey_west_t_stat(values: np.ndarray, *, lag: int) -> float | None:
    clean = np.asarray(values, dtype=float)
    clean = clean[np.isfinite(clean)]
    sample_count = int(clean.size)
    if sample_count < 3:
        return None
    mean = float(clean.mean())
    demeaned = clean - mean
    gamma_zero = float(np.dot(demeaned, demeaned) / sample_count)
    if gamma_zero <= 0:
        return None
    variance = gamma_zero
    max_lag = min(max(0, int(lag)), sample_count - 1)
    for offset in range(1, max_lag + 1):
        covariance = float(
            np.dot(demeaned[:-offset], demeaned[offset:]) / sample_count
        )
        weight = 1.0 - offset / (max_lag + 1)
        variance += 2.0 * weight * covariance
    if variance <= 0:
        variance = gamma_zero
    return float(mean / np.sqrt(variance / sample_count))


def _daily_rank_metrics(frame: pd.DataFrame, prediction: pd.Series) -> dict[str, float]:
    evaluated = frame[["trade_date", "instrument", "label"]].copy()
    evaluated["prediction"] = pd.to_numeric(prediction, errors="coerce")
    evaluated = evaluated.dropna(subset=["prediction", "label"])
    daily_ic: list[float] = []
    for _, group in evaluated.groupby("trade_date", sort=True):
        if group["prediction"].nunique() <= 1 or group["label"].nunique() <= 1:
            continue
        value = group["prediction"].corr(group["label"], method="spearman")
        if pd.notna(value):
            daily_ic.append(float(value))
    ic_mean = float(np.mean(daily_ic)) if daily_ic else 0.0
    ic_std = float(np.std(daily_ic, ddof=1)) if len(daily_ic) > 1 else 0.0
    rmse = float(np.sqrt(np.mean(np.square(
        evaluated["prediction"].to_numpy() - evaluated["label"].to_numpy()
    ))))
    return {
        "ic": ic_mean,
        "rank_ic": ic_mean,
        "ic_ir": ic_mean / ic_std if ic_std else 0.0,
        "rmse": rmse,
    }


def _rank_ensemble_score(
    frame: pd.DataFrame, source_keys: list[str], weights: np.ndarray,
) -> pd.Series:
    raw = frame[source_keys].to_numpy(dtype=float) @ np.asarray(weights, dtype=float)
    ranked = frame[["trade_date", "instrument"]].copy()
    ranked["raw"] = raw
    ranked = ranked.sort_values(
        ["trade_date", "raw", "instrument"], ascending=[True, True, False],
    )
    ranked["ascending_rank"] = ranked.groupby("trade_date").cumcount() + 1
    ranked["section_count"] = ranked.groupby("trade_date")["raw"].transform("size")
    ranked["score"] = np.where(
        ranked["section_count"] <= 1,
        0.0,
        2.0 * (ranked["ascending_rank"] - 1.0) / (ranked["section_count"] - 1.0) - 1.0,
    )
    return ranked["score"].reindex(frame.index)


def _ensemble_sensitivity_weights(weights: np.ndarray) -> list[np.ndarray]:
    if len(weights) == 2:
        return [np.asarray([left, 1.0 - left]) for left in np.linspace(0.0, 1.0, 5)]
    scenarios = [weights.copy()]
    for target in range(len(weights)):
        for delta in (-0.1, 0.1):
            target_weight = min(1.0, max(0.0, float(weights[target]) + delta))
            adjusted = weights.copy()
            others = [index for index in range(len(weights)) if index != target]
            other_total = float(weights[others].sum())
            adjusted[target] = target_weight
            if others:
                if other_total > 0:
                    adjusted[others] = weights[others] / other_total * (1.0 - target_weight)
                else:
                    adjusted[others] = (1.0 - target_weight) / len(others)
            if not any(np.allclose(adjusted, item) for item in scenarios):
                scenarios.append(adjusted)
    return scenarios


def _ensemble_time_stability(
    *, aligned: pd.DataFrame, sources: list[dict[str, Any]],
    source_keys: list[str], source_weights: np.ndarray,
) -> dict[str, Any]:
    dates = list(pd.Index(aligned["trade_date"].drop_duplicates()).sort_values())
    window_count = min(8, max(1, int(np.ceil(len(dates) / 20.0))))
    date_windows = [list(window) for window in np.array_split(dates, window_count)]
    windows: list[dict[str, Any]] = []
    source_window_values: dict[str, list[dict[str, float]]] = {
        key: [] for key in source_keys
    }
    for number, window_dates in enumerate(date_windows, start=1):
        if not window_dates:
            continue
        window = aligned[aligned["trade_date"].isin(window_dates)].copy()
        baseline_score = _rank_ensemble_score(window, source_keys, source_weights)
        baseline = _daily_rank_metrics(window, baseline_score)
        source_results: list[dict[str, Any]] = []
        for index, source in enumerate(sources):
            standalone_score = _rank_ensemble_score(
                window, [source_keys[index]], np.asarray([1.0]),
            )
            standalone = _daily_rank_metrics(window, standalone_score)
            keep = [position for position in range(len(source_keys)) if position != index]
            if len(keep) == 1:
                without_score = _rank_ensemble_score(
                    window, [source_keys[keep[0]]], np.asarray([1.0]),
                )
            else:
                remaining_weights = source_weights[keep]
                remaining_weights = remaining_weights / remaining_weights.sum()
                without_score = _rank_ensemble_score(
                    window,
                    [source_keys[position] for position in keep],
                    remaining_weights,
                )
            without = _daily_rank_metrics(window, without_score)
            marginal_rank_ic = baseline["rank_ic"] - without["rank_ic"]
            source_result = {
                "source_key": source_keys[index],
                "standalone_rank_ic": standalone["rank_ic"],
                "standalone_ic_ir": standalone["ic_ir"],
                "marginal_rank_ic": marginal_rank_ic,
            }
            source_results.append(source_result)
            source_window_values[source_keys[index]].append(source_result)
        windows.append({
            "window": number,
            "date_start": pd.Timestamp(window_dates[0]).date(),
            "date_end": pd.Timestamp(window_dates[-1]).date(),
            "days": int(window["trade_date"].nunique()),
            "rows": int(len(window)),
            "rank_ic": baseline["rank_ic"],
            "ic_ir": baseline["ic_ir"],
            "average_source_correlation": _average_daily_pairwise_correlation(
                window, source_keys,
            ),
            "sources": source_results,
        })
    rank_ics = np.asarray([float(item["rank_ic"]) for item in windows], dtype=float)
    positive_ratio = float(np.mean(rank_ics > 0)) if len(rank_ics) else 0.0
    rank_ic_std = float(np.std(rank_ics, ddof=1)) if len(rank_ics) > 1 else 0.0
    if len(windows) < 3:
        status = "insufficient_windows"
        conclusion = "样本外窗口不足3个，暂不能判断时间稳定性"
    elif positive_ratio >= 2 / 3 and float(rank_ics.min()) > 0 and rank_ic_std <= 0.03:
        status = "stable"
        conclusion = "融合模型在各时间窗口方向一致，RankIC波动处于可接受范围"
    elif positive_ratio >= 0.5:
        status = "mixed"
        conclusion = "融合模型多数窗口有效，但跨期表现仍有明显波动"
    else:
        status = "unstable"
        conclusion = "融合模型跨期方向不稳定，需要增加训练窗口或调整源模型结构"
    source_stability: list[dict[str, Any]] = []
    for source in sources:
        key = str(source["source_key"])
        values = source_window_values[key]
        marginals = np.asarray(
            [float(item["marginal_rank_ic"]) for item in values], dtype=float,
        )
        standalone = np.asarray(
            [float(item["standalone_rank_ic"]) for item in values], dtype=float,
        )
        source_stability.append({
            **source,
            "positive_marginal_windows": int(np.sum(marginals > 0)),
            "positive_marginal_window_ratio": (
                float(np.mean(marginals > 0)) if len(marginals) else 0.0
            ),
            "marginal_rank_ic_mean": float(np.mean(marginals)) if len(marginals) else 0.0,
            "marginal_rank_ic_std": (
                float(np.std(marginals, ddof=1)) if len(marginals) > 1 else 0.0
            ),
            "standalone_rank_ic_mean": (
                float(np.mean(standalone)) if len(standalone) else 0.0
            ),
        })
    return {
        "policy": "approximately_20_oos_trading_days_per_window",
        "status": status,
        "conclusion": conclusion,
        "window_count": len(windows),
        "positive_rank_ic_window_ratio": positive_ratio,
        "rank_ic_mean": float(np.mean(rank_ics)) if len(rank_ics) else 0.0,
        "rank_ic_std": rank_ic_std,
        "rank_ic_min": float(np.min(rank_ics)) if len(rank_ics) else 0.0,
        "rank_ic_max": float(np.max(rank_ics)) if len(rank_ics) else 0.0,
        "windows": windows,
        "sources": source_stability,
    }


def _average_daily_pairwise_correlation(
    frame: pd.DataFrame, source_keys: list[str],
) -> float:
    values: list[float] = []
    for left in range(len(source_keys)):
        for right in range(left + 1, len(source_keys)):
            daily: list[float] = []
            for _, group in frame.groupby("trade_date", sort=True):
                if (
                    group[source_keys[left]].nunique() <= 1
                    or group[source_keys[right]].nunique() <= 1
                ):
                    continue
                value = group[source_keys[left]].corr(
                    group[source_keys[right]], method="spearman",
                )
                if pd.notna(value):
                    daily.append(float(value))
            if daily:
                values.append(float(np.mean(daily)))
    return float(np.mean(values)) if values else 0.0


def _source_key(model_id: str, version: int) -> str:
    return f"{model_id}::v{int(version)}"


def _diagnostic_source_view(source: Mapping[str, Any]) -> dict[str, Any]:
    model_id = str(source.get("model_id") or "")
    model_version = int(source.get("model_version") or source.get("version") or 0)
    return {
        "source_key": _source_key(model_id, model_version),
        "model_id": model_id,
        "model_version": model_version,
        "name": str(source.get("name") or model_id),
        "model_kind": str(source.get("model_kind") or ""),
        "weight": float(source.get("weight") or 0.0),
    }


def _ensemble_source_condition(
    sources: list[Mapping[str, Any]],
) -> tuple[str, dict[str, Any]]:
    if not 2 <= len(sources) <= 8:
        raise ValueError("融合模型必须包含2到8个源模型")
    clauses: list[str] = []
    parameters: dict[str, Any] = {"source_count": len(sources)}
    seen: set[tuple[str, int]] = set()
    for index, source in enumerate(sources):
        model_id = str(source.get("model_id") or "").strip()
        version = int(source.get("model_version") or source.get("version") or 0)
        key = (model_id, version)
        if not model_id or version <= 0 or key in seen:
            raise ValueError("源模型版本无效或重复")
        seen.add(key)
        parameters[f"source_model_id_{index}"] = model_id
        parameters[f"source_model_version_{index}"] = version
        clauses.append(
            f"(model_id = {{source_model_id_{index}:String}} AND "
            f"model_version = {{source_model_version_{index}:UInt32}})"
        )
    return " OR ".join(clauses), parameters


def model_paper_snapshot(
    *, model_id: str, model_version: int, execution_date: date,
    current_codes: list[str], top_n: int = 20,
) -> dict:
    """Resolve the last close signal and today's executable open snapshot."""
    database = settings().model_database
    signal_date = client().query(
        f"""
        SELECT max(trade_date)
        FROM {database}.model_predictions_daily FINAL
        WHERE model_id = {{model_id:String}}
          AND model_version = {{model_version:UInt32}}
          AND trade_date < {{execution_date:Date}}
          AND feature_cutoff_at <= toDateTime(trade_date, 'Asia/Shanghai') + INTERVAL 15 HOUR
        """,
        parameters={
            "model_id": model_id, "model_version": int(model_version),
            "execution_date": execution_date,
        },
    ).result_rows[0][0]
    if signal_date is None:
        raise ValueError("执行日前没有可用模型收盘信号")
    signals = list_model_signals(
        model_id=model_id, model_version=model_version,
        trade_date=signal_date, top_n=top_n,
    )
    target_codes = [row.entity_code for row in signals]
    codes = sorted(set(target_codes) | {str(code) for code in current_codes if str(code)})
    rows = client().query(
        """
        SELECT k.code, k.open, k.open * ifNull(a.backward_adj_factor, 1.0) AS adjusted_open,
               toUInt8(ifNull(s.is_susp_sec, '') IN ('1','true','True')) AS is_suspended,
               s.high_limited, s.low_limited
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
          AND toDate(k.trade_time) = {execution_date:Date}
        ORDER BY k.code
        """,
        parameters={"codes": codes, "execution_date": execution_date},
    ).result_rows
    market = {}
    for code, raw_open, price, suspended, high_limit, low_limit in rows:
        value = float(price or 0.0)
        open_value = float(raw_open or 0.0)
        market[str(code)] = {
            "price": value,
            "buy_allowed": bool(value > 0 and not suspended and not (high_limit and open_value >= float(high_limit) - 1e-8)),
            "sell_allowed": bool(value > 0 and not suspended and not (low_limit and open_value <= float(low_limit) + 1e-8)),
        }
    return {
        "model_id": model_id,
        "model_version": int(model_version),
        "signal_date": signal_date,
        "execution_date": execution_date,
        "targets": [
            {
                "entity_code": row.entity_code, "score": row.score,
                "rank_value": row.rank_value,
            }
            for row in signals
        ],
        "market": market,
    }


def model_inference_availability(
    *, factors: list[dict], requested_trade_date: Optional[date] = None,
    data_cutoff: Optional[datetime] = None,
) -> dict:
    if not factors:
        raise ValueError("模型没有冻结因子")
    effective_cutoff = data_cutoff or datetime.now()
    _validate_frozen_factors(factors)
    available_through = _available_market_date(effective_cutoff)
    market_columns = "max(toDate(trade_time))"
    market_params: dict[str, object] = {"available_through": available_through}
    if requested_trade_date:
        market_columns += ", toUInt8(countIf(toDate(trade_time) = {requested_trade_date:Date}) > 0)"
        market_params["requested_trade_date"] = requested_trade_date
    market_row = client().query(
        f"""
        SELECT {market_columns}
        FROM starlight.ad_market_kline_daily
        WHERE code = '000905.SH'
          AND toDate(trade_time) <= {{available_through:Date}}
        """,
        parameters=market_params,
    ).result_rows[0]
    market_latest = market_row[0]
    ready_dates = _factor_source_ready_dates(
        factors=factors,
        after_date=None,
        before_date=market_latest,
        limit=1,
        descending=True,
    )
    factor_latest = ready_dates[0] if ready_dates else None
    common_latest = factor_latest
    requested_available = None
    if requested_trade_date:
        requested_ready = _factor_source_ready_dates(
            factors=factors,
            after_date=date.fromordinal(requested_trade_date.toordinal() - 1),
            before_date=requested_trade_date,
            limit=1,
            descending=False,
        )
        requested_available = (
            requested_trade_date <= available_through
            and bool(market_row[1])
            and requested_trade_date in requested_ready
        )
    return {
        "trade_date": common_latest,
        "factor_latest_date": factor_latest,
        "market_latest_date": market_latest,
        "factor_count": len(factors),
        "requested_trade_date": requested_trade_date,
        "requested_trade_date_available": requested_available,
        "data_cutoff": effective_cutoff,
    }


def model_inference_dates(
    *, factors: list[dict], after_date: date, before_date: Optional[date] = None,
    data_cutoff: Optional[datetime] = None, limit: int = 20,
) -> list[date]:
    """Return PIT-safe market dates for which every frozen factor is available."""
    if not factors:
        raise ValueError("模型没有冻结因子")
    effective_cutoff = data_cutoff or datetime.now()
    _validate_frozen_factors(factors)
    available_through = _available_market_date(effective_cutoff)
    return _factor_source_ready_dates(
        factors=factors,
        after_date=after_date,
        before_date=min(before_date or date.max, available_through),
        limit=max(1, min(int(limit), 250)),
        descending=False,
    )


def _factor_source_ready_dates(
    *,
    factors: list[dict],
    after_date: Optional[date],
    before_date: date,
    limit: int,
    descending: bool,
    minimum_coverage: float = 0.8,
) -> list[date]:
    """Return benchmark dates whose raw inputs cover every frozen factor.

    This is a cheap preflight before formula evaluation. The inference worker
    still performs the authoritative score coverage check, but incomplete
    source vintages (for example a partially-loaded latest day) are skipped
    before a job is submitted or scheduled.
    """
    config = settings()
    source_database = _source_identifier(config.source_database, "source database")
    source_table = _source_identifier(config.stock_daily_table, "source table")
    code_column = _source_identifier(config.stock_code_column, "source code column")
    date_column = _source_identifier(config.stock_date_column, "source date column")
    coverage_checks: list[str] = []
    for index, item in enumerate(factors):
        factor = factor_repository.get_factor(
            str(item.get("factor_id") or ""),
            version=int(item.get("factor_version") or 0),
        )
        if factor is None:
            raise ValueError(
                f"冻结因子不存在: {item.get('factor_id')} v{item.get('factor_version')}"
            )
        fields = [
            _source_identifier(str(field), f"因子{factor.factor_id}源字段")
            for field in factor.required_fields
        ]
        field_ready = " AND ".join(
            f"isNotNull(toFloat64OrNull(nullIf(toString(source.{field}), '')))"
            for field in fields
        ) or f"isNotNull(source.{code_column})"
        coverage_checks.append(
            f"countIf({field_ready}) / greatest(count(), 1) "
            f">= {{minimum_coverage_{index}:Float64}}"
        )
    direction = "DESC" if descending else "ASC"
    candidate_limit = max(250, min(1000, int(limit) * 5))
    source_after = after_date or (
        before_date - timedelta(days=candidate_limit * 2 + 30)
    )
    after_filter = (
        "AND toDate(trade_time) > {after_date:Date}" if after_date else ""
    )
    having = " AND ".join(coverage_checks) or "count() > 0"
    params: dict[str, object] = {
        "before_date": before_date,
        "source_after": source_after,
        "candidate_limit": candidate_limit,
        "limit": max(1, min(int(limit), 250)),
        **({"after_date": after_date} if after_date else {}),
        **{
            f"minimum_coverage_{index}": float(minimum_coverage)
            for index in range(len(coverage_checks))
        },
    }
    rows = client().query(
        f"""
        WITH calendar AS (
            SELECT DISTINCT toDate(trade_time) AS trade_date
            FROM starlight.ad_market_kline_daily
            WHERE code = '000905.SH'
              {after_filter}
              AND toDate(trade_time) <= {{before_date:Date}}
            ORDER BY trade_date {direction}
            LIMIT {{candidate_limit:UInt32}}
        ), membership AS (
            SELECT calendar.trade_date, members.con_code
            FROM calendar
            CROSS JOIN (
                SELECT con_code, in_date, out_date
                FROM starlight.ad_index_constituent
                WHERE index_code = '000905.SH'
                  AND in_date <= {{before_date:Date}}
            ) AS members
            WHERE members.in_date <= calendar.trade_date
              AND (members.out_date IS NULL OR members.out_date >= calendar.trade_date)
        )
        SELECT membership.trade_date
        FROM membership
        LEFT JOIN (
            SELECT *
            FROM {source_database}.{source_table}
            WHERE toDate({date_column}) > {{source_after:Date}}
              AND toDate({date_column}) <= {{before_date:Date}}
        ) AS source
          ON source.{code_column} = membership.con_code
         AND toDate(source.{date_column}) = membership.trade_date
        GROUP BY membership.trade_date
        HAVING {having}
        ORDER BY membership.trade_date {direction}
        LIMIT {{limit:UInt32}}
        """,
        parameters=params,
    ).result_rows
    return [row[0] for row in rows]


def _validate_frozen_factors(factors: list[dict]) -> None:
    for item in factors:
        factor_id = str(item.get("factor_id") or "")
        version = int(item.get("factor_version") or 0)
        params = item.get("params")
        if not isinstance(params, dict):
            raise ValueError(f"冻结因子{factor_id}缺少params")
        factor = factor_repository.get_factor(factor_id, version=version)
        if factor is None:
            raise ValueError(f"冻结因子不存在: {factor_id} v{version}")
        if factor_params_hash(factor, params) != str(item.get("params_hash") or ""):
            raise ValueError(f"冻结因子{factor_id}的params_hash与公式参数不一致")


def _source_identifier(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"{label}不是安全标识符")
    return value


def _available_market_date(cutoff: datetime) -> date:
    localized = cutoff
    if cutoff.tzinfo is not None:
        localized = cutoff.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    available = localized.date()
    if localized.hour < 15:
        available = date.fromordinal(available.toordinal() - 1)
    return available


def create_model_backtest_job(payload: ModelBacktestJobCreate) -> ModelBacktestJobOut:
    if payload.universe_id not in UNIVERSES:
        raise ValueError("不支持的股票池")
    if payload.date_preset == "custom":
        if not payload.date_start or not payload.date_end or payload.date_start >= payload.date_end:
            raise ValueError("自定义回测必须提供有效日期范围")
    database = settings().model_database
    exists = client().query(
        f"""
        SELECT count()
        FROM {database}.model_predictions_daily
        WHERE model_id = {{model_id:String}} AND model_version = {{model_version:UInt32}}
        """,
        parameters={"model_id": payload.model_id, "model_version": payload.model_version},
    ).result_rows[0][0]
    if not exists:
        raise ValueError("模型版本没有预测结果")
    now = datetime.now()
    backtest_job_id = f"model_backtest_{uuid4().hex}"
    configuration = {
        "signal_time": "trade_date_close",
        "execution_time": "next_trade_date_open",
        "execution_price": "next_open_backward_adjusted",
        "portfolio": "top_n_equal_weight",
        "blocked_trades_are_carried": True,
        "exclude_limit_paused": False,
        "exclude_st": False,
        "exclude_new_stocks": False,
        "exclude_delisting": False,
        "exclude_bse": False,
        "research_only": bool(payload.research_only),
    }
    row = [
        backtest_job_id, payload.model_id, payload.model_version,
        payload.universe_id, UNIVERSES[payload.universe_id]["benchmark"],
        payload.date_preset, payload.date_start, payload.date_end, None, None,
        payload.top_n, payload.rebalance_every, 0.0003, 0.0013,
        json.dumps(configuration, ensure_ascii=False, sort_keys=True),
        "pending", "", None, None, None, None, None, 0, "{}",
        now, None, None, now,
    ]
    client().insert(
        f"{database}.model_backtest_jobs", [row],
        column_names=_MODEL_JOB_COLUMNS,
    )
    return get_model_backtest_job(backtest_job_id)  # type: ignore[return-value]


def create_architecture_backtest_job(
    architecture: Mapping[str, Any], *, date_preset: str = "3y",
    date_start: Optional[date] = None, date_end: Optional[date] = None,
    ablation_profile: str = "full",
) -> ModelBacktestJobOut:
    """Create a research backtest for an immutable architecture snapshot.

    Architecture jobs reuse the existing model backtest storage and execution
    engine.  ``model_id`` stores the architecture id and ``model_version`` the
    architecture revision; ``configuration.signal_source`` is the explicit
    discriminator that prevents the job from being mistaken for a model
    validation backtest.
    """
    architecture_id = str(architecture.get("architecture_id") or "").strip()
    revision = int(architecture.get("revision") or 0)
    if not architecture_id or revision < 1:
        raise ValueError("模型架构身份不完整")
    if str(architecture.get("state") or "draft") == "archived":
        raise ValueError("已归档模型架构不能创建回测")
    readiness = dict(architecture.get("readiness") or {})
    if readiness.get("research_backtest_ready") is not True:
        raise ValueError("模型架构尚未通过研究回测的数据与预测就绪检查")
    all_engines = [
        dict(item) for item in architecture.get("engines") or []
        if item.get("enabled") is True
    ]
    if not all_engines:
        raise ValueError("模型架构没有启用的引擎")
    profile_key = str(ablation_profile or "full").strip().lower()
    profile = ARCHITECTURE_ABLATION_PROFILES.get(profile_key)
    if profile is None:
        raise ValueError("不支持的架构消融方案")
    architecture_pipeline = str(architecture.get("pipeline_mode") or "flat")
    if profile_key != "full" and architecture_pipeline != "hierarchical":
        raise ValueError("只有三级门控架构支持分层消融回测")

    def engine_stage(item: Mapping[str, Any]) -> str:
        frozen = str(item.get("stage") or "").strip()
        if frozen:
            return frozen
        return {
            "market_style": "style_gate",
            "industry_rotation": "industry_gate",
            "risk_filter": "risk_gate",
        }.get(str(item.get("role") or "stock_selection"), "stock_rank")

    engines = (
        all_engines
        if profile_key == "full"
        else [
            item for item in all_engines
            if engine_stage(item) in profile["stages"]
        ]
    )
    stage_counts = {
        stage: sum(engine_stage(item) == stage for item in engines)
        for stage in ("style_gate", "industry_gate", "stock_rank")
    }
    if stage_counts["stock_rank"] < 1:
        raise ValueError("消融回测至少需要一个个股排序引擎")
    if profile_key == "style_stock" and stage_counts["style_gate"] != 1:
        raise ValueError("风格消融缺少市场风格引擎")
    if profile_key == "industry_stock" and stage_counts["industry_gate"] < 1:
        raise ValueError("行业消融缺少行业轮动引擎")
    universe_id = str(architecture.get("universe_id") or "")
    if universe_id not in UNIVERSES:
        raise ValueError("模型架构使用了不支持的股票池")
    request = ModelBacktestJobCreate(
        model_id=architecture_id,
        model_version=revision,
        universe_id=universe_id,
        date_preset=date_preset,
        date_start=date_start,
        date_end=date_end,
        top_n=int(architecture.get("top_n") or 20),
        rebalance_every=int(architecture.get("rebalance_every") or 5),
    )
    if request.date_preset == "custom" and (
        not request.date_start or not request.date_end
        or request.date_start >= request.date_end
    ):
        raise ValueError("自定义回测必须提供有效日期范围")
    source_models = list(dict.fromkeys(
        (
            str(item.get("model_id") or ""),
            int(item.get("model_version") or 0),
        )
        for item in engines
    ))
    available = client().query(
        f"""
        SELECT countDistinct((model_id, model_version))
        FROM {settings().model_database}.model_predictions_daily
        WHERE (model_id, model_version) IN
              {{models:Array(Tuple(String, UInt32))}}
        """,
        parameters={"models": source_models},
    ).result_rows[0][0]
    if int(available or 0) != len(source_models):
        raise ValueError("模型架构至少有一个引擎缺少预测结果")
    now = datetime.now()
    backtest_job_id = f"architecture_backtest_{uuid4().hex}"
    configuration = {
        "signal_source": "model_architecture",
        "architecture_id": architecture_id,
        "architecture_revision": revision,
        "architecture_fingerprint": str(architecture.get("fingerprint") or ""),
        "pipeline_mode": (
            architecture_pipeline
            if profile_key == "full" else str(profile["pipeline_mode"])
        ),
        "merge_method": str(architecture.get("merge_method") or "priority"),
        "engines": engines,
        "walk_forward": _architecture_backtest_walk_forward_contract(engines),
        "ablation_profile": profile_key,
        "ablation_label": str(profile["label"]),
        "signal_time": "trade_date_close",
        "execution_time": "next_trade_date_open",
        "execution_price": "next_open_backward_adjusted",
        "portfolio": "architecture_top_n_equal_weight",
        "blocked_trades_are_carried": True,
        "exclude_limit_paused": False,
        "exclude_st": False,
        "exclude_new_stocks": False,
        "exclude_delisting": False,
        "exclude_bse": False,
    }
    row = [
        backtest_job_id, architecture_id, revision,
        universe_id, UNIVERSES[universe_id]["benchmark"],
        request.date_preset, request.date_start, request.date_end, None, None,
        request.top_n, request.rebalance_every, 0.0003, 0.0013,
        json.dumps(configuration, ensure_ascii=False, sort_keys=True),
        "pending", "", None, None, None, None, None, 0, "{}",
        now, None, None, now,
    ]
    client().insert(
        f"{settings().model_database}.model_backtest_jobs", [row],
        column_names=_MODEL_JOB_COLUMNS,
    )
    return get_model_backtest_job(backtest_job_id)  # type: ignore[return-value]


def get_model_backtest_job(backtest_job_id: str) -> Optional[ModelBacktestJobOut]:
    database = settings().model_database
    rows = client().query(
        f"SELECT * FROM {database}.model_backtest_jobs FINAL WHERE backtest_job_id = {{id:String}} LIMIT 1",
        parameters={"id": backtest_job_id},
    ).result_rows
    return _job_from_row(rows[0]) if rows else None


def list_architecture_backtest_jobs(
    architecture_id: str, revision: int, *, limit: int = 40,
) -> list[ModelBacktestJobOut]:
    rows = client().query(
        f"""
        SELECT *
        FROM {settings().model_database}.model_backtest_jobs FINAL
        WHERE model_id = {{architecture_id:String}}
          AND model_version = {{revision:UInt32}}
        ORDER BY created_at DESC, updated_at DESC
        LIMIT {{limit:UInt32}}
        """,
        parameters={
            "architecture_id": str(architecture_id),
            "revision": int(revision),
            "limit": max(1, min(int(limit), 200)),
        },
    ).result_rows
    return [
        job for job in (_job_from_row(row) for row in rows)
        if job.configuration.get("signal_source") == "model_architecture"
    ]


def latest_model_backtests(
    models: list[tuple[str, int]],
) -> dict[tuple[str, int], ModelBacktestJobOut]:
    """Return the latest successful TopN backtest for each requested model."""
    keys = list(dict.fromkeys((str(model_id), int(version)) for model_id, version in models))
    if not keys:
        return {}
    database = settings().model_database
    rows = client().query(
        f"""
        SELECT *
        FROM {database}.model_backtest_jobs FINAL
        WHERE status = 'success'
          AND (model_id, model_version) IN {{models:Array(Tuple(String, UInt32))}}
        ORDER BY finished_at DESC, created_at DESC
        """,
        parameters={"models": keys},
    ).result_rows
    result: dict[tuple[str, int], ModelBacktestJobOut] = {}
    for row in rows:
        backtest = _job_from_row(row)
        if backtest.configuration.get("research_only") is True:
            continue
        profile = str(backtest.configuration.get("ablation_profile") or "full")
        if profile != "full":
            continue
        result.setdefault((backtest.model_id, backtest.model_version), backtest)
    return result


def latest_model_backtest_jobs(
    identities: list[tuple[str, int]],
) -> dict[tuple[str, int], ModelBacktestJobOut]:
    """Return the newest backtest job regardless of terminal state."""
    keys = list(dict.fromkeys(
        (str(identity), int(revision)) for identity, revision in identities
    ))
    if not keys:
        return {}
    rows = client().query(
        f"""
        SELECT *
        FROM {settings().model_database}.model_backtest_jobs FINAL
        WHERE (model_id, model_version) IN
              {{identities:Array(Tuple(String, UInt32))}}
        ORDER BY created_at DESC, updated_at DESC
        """,
        parameters={"identities": keys},
    ).result_rows
    result: dict[tuple[str, int], ModelBacktestJobOut] = {}
    for row in rows:
        backtest = _job_from_row(row)
        if backtest.configuration.get("research_only") is True:
            continue
        profile = str(backtest.configuration.get("ablation_profile") or "full")
        if profile != "full":
            continue
        result.setdefault((backtest.model_id, backtest.model_version), backtest)
    return result


def list_model_sensitivity_backtests(
    model_id: str,
    model_version: int,
    *,
    limit: int = 20,
) -> list[ModelBacktestJobOut]:
    rows = client().query(
        f"""
        SELECT *
        FROM {settings().model_database}.model_backtest_jobs FINAL
        WHERE model_id = {{model_id:String}}
          AND model_version = {{model_version:UInt32}}
        ORDER BY created_at DESC, updated_at DESC
        LIMIT {{limit:UInt32}}
        """,
        parameters={
            "model_id": str(model_id),
            "model_version": int(model_version),
            "limit": max(1, min(int(limit), 100)),
        },
    ).result_rows
    return [
        job for job in (_job_from_row(row) for row in rows)
        if job.configuration.get("research_only") is True
    ]


def update_model_backtest_job(backtest_job_id: str, **changes) -> ModelBacktestJobOut:
    current = get_model_backtest_job(backtest_job_id)
    if current is None:
        raise ValueError("模型回测任务不存在")
    now = datetime.now()
    values = current.model_dump()
    values.update({key: value for key, value in changes.items() if value is not None})
    row = [
        current.backtest_job_id, current.model_id, current.model_version,
        current.universe_id, current.benchmark_code, current.date_preset,
        current.requested_date_start, current.requested_date_end,
        values.get("date_start"), values.get("date_end"), current.top_n,
        current.rebalance_every, current.buy_cost_rate, current.sell_cost_rate,
        json.dumps(current.configuration, ensure_ascii=False, sort_keys=True),
        values["status"], values.get("error_message", ""),
        values.get("annual_return"), values.get("excess_annual_return"),
        values.get("sharpe_ratio"), values.get("turnover_rate"),
        values.get("max_drawdown"), values.get("trading_days", 0),
        json.dumps(values.get("payload") or {}, ensure_ascii=False, sort_keys=True),
        current.created_at or now, values.get("started_at"), values.get("finished_at"), now,
    ]
    client().insert(f"{settings().model_database}.model_backtest_jobs", [row], column_names=_MODEL_JOB_COLUMNS)
    return get_model_backtest_job(backtest_job_id)  # type: ignore[return-value]


def replace_model_backtest_daily(backtest_job_id: str, rows: list[tuple]) -> None:
    database = settings().model_database
    client().command(
        f"ALTER TABLE {database}.model_backtest_daily DELETE WHERE backtest_job_id = {{id:String}} SETTINGS mutations_sync = 2",
        parameters={"id": backtest_job_id},
    )
    if rows:
        now = datetime.now()
        client().insert(
            f"{database}.model_backtest_daily",
            [list(row) + [now] for row in rows],
            column_names=[
                "backtest_job_id", "trade_date", "portfolio_return",
                "benchmark_return", "excess_return", "portfolio_nav",
                "benchmark_nav", "turnover", "transaction_cost", "sample_count",
                "holding_count", "blocked_buy_count", "blocked_sell_count",
                "holdings_json", "updated_at",
            ],
        )


def list_model_backtest_daily(backtest_job_id: str, limit: int = 5000) -> list[ModelBacktestDailyOut]:
    rows = client().query(
        f"""
        SELECT * FROM {settings().model_database}.model_backtest_daily FINAL
        WHERE backtest_job_id = {{id:String}}
        ORDER BY trade_date LIMIT {{limit:UInt32}}
        """,
        parameters={"id": backtest_job_id, "limit": max(1, min(limit, 20000))},
    ).result_rows
    return [
        ModelBacktestDailyOut(
            backtest_job_id=row[0], trade_date=row[1], portfolio_return=row[2],
            benchmark_return=row[3], excess_return=row[4], portfolio_nav=row[5],
            benchmark_nav=row[6], turnover=row[7], transaction_cost=row[8],
            sample_count=row[9], holding_count=row[10], blocked_buy_count=row[11],
            blocked_sell_count=row[12], holdings=json.loads(row[13] or "[]"),
            updated_at=row[14],
        )
        for row in rows
    ]


_MODEL_JOB_COLUMNS = [
    "backtest_job_id", "model_id", "model_version", "universe_id",
    "benchmark_code", "date_preset", "requested_date_start",
    "requested_date_end", "date_start", "date_end", "top_n",
    "rebalance_every", "buy_cost_rate", "sell_cost_rate", "configuration_json",
    "status", "error_message", "annual_return", "excess_annual_return",
    "sharpe_ratio", "turnover_rate", "max_drawdown", "trading_days",
    "payload_json", "created_at", "started_at", "finished_at", "updated_at",
]


def _job_from_row(row) -> ModelBacktestJobOut:
    return ModelBacktestJobOut(
        backtest_job_id=row[0], model_id=row[1], model_version=row[2],
        universe_id=row[3], benchmark_code=row[4], date_preset=row[5],
        requested_date_start=row[6], requested_date_end=row[7], date_start=row[8],
        date_end=row[9], top_n=row[10], rebalance_every=row[11],
        buy_cost_rate=row[12], sell_cost_rate=row[13],
        configuration=json.loads(row[14] or "{}"), status=row[15],
        error_message=row[16], annual_return=row[17], excess_annual_return=row[18],
        sharpe_ratio=row[19], turnover_rate=row[20], max_drawdown=row[21],
        trading_days=row[22], payload=json.loads(row[23] or "{}"),
        created_at=row[24], started_at=row[25], finished_at=row[26], updated_at=row[27],
    )
