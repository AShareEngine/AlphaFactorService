from __future__ import annotations

from functools import lru_cache
import gc
import json
from pathlib import Path
import pickle
import re
import sqlite3
import subprocess
import sys
import tarfile
from typing import Any, Mapping

import numpy as np
import pandas as pd


_DATASET_HASH = re.compile(r"^[0-9a-f]{64}$")


def dataset_feature_drift(
    dataset_hash: str,
    artifact_root: str | Path,
) -> dict[str, Any]:
    """Compare the immutable train and test feature distributions.

    Diagnostics are read from the raw, pre-imputation snapshot so missing-value
    changes remain visible. The cache key includes the resolved artifact root and
    dataset hash; dataset snapshots are immutable by contract.
    """
    clean_hash = str(dataset_hash or "").strip().lower()
    if not _DATASET_HASH.fullmatch(clean_hash):
        raise ValueError("dataset_hash必须是64位十六进制摘要")
    root = Path(artifact_root).resolve()
    return _cached_dataset_feature_drift(clean_hash, root.as_posix())


def dataset_walk_forward_attribution(
    dataset_hash: str,
    artifact_root: str | Path,
    walk_forward: Mapping[str, Any],
) -> dict[str, Any]:
    """Explain each frozen WFA window with factor IC and distribution drift.

    The calculation only reads the immutable raw dataset snapshot.  Test-window
    statistics are explanatory diagnostics and are deliberately marked as
    unsuitable for selecting or refitting the current model version.
    """
    clean_hash = str(dataset_hash or "").strip().lower()
    if not _DATASET_HASH.fullmatch(clean_hash):
        raise ValueError("dataset_hash必须是64位十六进制摘要")
    windows = []
    for raw in list(dict(walk_forward or {}).get("windows") or []):
        segments = dict(raw.get("segments") or {})
        train = _json_segment(segments.get("train"), "train")
        test = _json_segment(segments.get("test"), "test")
        metrics = dict(raw.get("metrics") or {})
        windows.append({
            "window": int(raw.get("window") or len(windows) + 1),
            "train": train,
            "test": test,
            "model_rank_ic": _optional_float(
                metrics.get("rank_ic", metrics.get("ic")),
            ),
            "model_ic_ir": _optional_float(metrics.get("ic_ir")),
        })
    if not windows:
        raise ValueError("模型没有冻结的WFA测试窗口")
    windows.sort(key=lambda item: item["window"])
    root = Path(artifact_root).resolve()
    return _cached_dataset_walk_forward_attribution(
        clean_hash,
        root.as_posix(),
        json.dumps(windows, ensure_ascii=False, sort_keys=True),
    )


def architecture_walk_forward_attribution(
    backtests: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Attribute aligned WFA backtests to industry and optional risk gates."""
    profile_labels = {
        "stock_only": "仅个股",
        "industry_stock": "行业 + 个股",
        "full": "分层全开",
    }
    selected: dict[str, dict[str, Any]] = {}
    for raw in backtests:
        item = dict(raw)
        configuration = dict(item.get("configuration") or {})
        profile = str(configuration.get("ablation_profile") or "full")
        report = dict((item.get("payload") or {}).get("walk_forward") or {})
        if (
            profile not in profile_labels
            or str(item.get("status") or "") != "success"
            or not report.get("windows")
            or profile in selected
        ):
            continue
        selected[profile] = {**item, "report": report}
    missing = [key for key in profile_labels if key not in selected]
    if missing:
        return {
            "eligible": False,
            "reason": "缺少完整成功消融结果：" + "、".join(
                profile_labels[key] for key in missing
            ),
            "profiles": [],
            "windows": [],
        }

    window_maps = {
        profile: {
            int(row.get("window") or 0): dict(row)
            for row in item["report"].get("windows") or []
            if row.get("complete") is True
        }
        for profile, item in selected.items()
    }
    common_windows = sorted(set.intersection(*(
        set(rows) for rows in window_maps.values()
    )))
    if not common_windows:
        return {
            "eligible": False,
            "reason": "三组消融没有共同完整WFA窗口",
            "profiles": [],
            "windows": [],
        }

    rows = []
    for window in common_windows:
        values = {
            profile: _optional_float(
                window_maps[profile][window].get("excess_annual_return"),
            )
            for profile in profile_labels
        }
        annual = [
            _optional_float(window_maps[profile][window].get("annual_return"))
            for profile in profile_labels
        ]
        benchmark_candidates = [
            value - values[profile]
            for value, profile in zip(annual, profile_labels)
            if value is not None and values[profile] is not None
        ]
        benchmark_annual = (
            float(np.mean(benchmark_candidates))
            if benchmark_candidates else None
        )
        finite_excess = [value for value in values.values() if value is not None]
        source = window_maps["full"][window]
        rows.append({
            "window": window,
            "test_start": source.get("test_start"),
            "test_end": source.get("test_end"),
            "benchmark_annual_return": benchmark_annual,
            "market_regime": _market_regime(benchmark_annual),
            "profiles": values,
            "average_excess_annual_return": (
                float(np.mean(finite_excess)) if finite_excess else None
            ),
            "all_profiles_negative": bool(finite_excess) and all(
                value < 0 for value in finite_excess
            ),
            "industry_gate_delta": _difference(
                values["industry_stock"], values["stock_only"],
            ),
            "full_vs_industry_delta": _difference(
                values["full"], values["industry_stock"],
            ),
        })
    weak_window = min(
        rows,
        key=lambda item: float(item["average_excess_annual_return"] or 0.0),
    )
    mean_for = lambda key: float(np.mean([
        row["profiles"][key] for row in rows
        if row["profiles"][key] is not None
    ]))
    profile_rows = [{
        "key": key,
        "label": label,
        "mean_excess_annual_return": mean_for(key),
        "status": selected[key]["report"].get("status"),
    } for key, label in profile_labels.items()]
    best_profile = max(
        profile_rows, key=lambda item: item["mean_excess_annual_return"],
    )
    common_failure_count = sum(row["all_profiles_negative"] for row in rows)
    conclusion = (
        f"共同最弱窗口为W{weak_window['window']}（{weak_window['test_start']}至"
        f"{weak_window['test_end']}）；三组方案均为负超额，说明问题不只来自单一门控。"
        if weak_window["all_profiles_negative"] else
        f"最弱窗口为W{weak_window['window']}，不同门控方案表现分化。"
    )
    return {
        "eligible": True,
        "policy": "alphablocks.architecture-wfa-attribution.v1",
        "window_count": len(rows),
        "common_failure_window_count": common_failure_count,
        "profiles": profile_rows,
        "best_profile": best_profile,
        "gate_contributions": {
            "industry_vs_stock_mean": mean_for("industry_stock") - mean_for("stock_only"),
            "full_vs_industry_mean": mean_for("full") - mean_for("industry_stock"),
        },
        "weak_window": weak_window,
        "windows": rows,
        "conclusion": conclusion,
        "guard": (
            "归因使用冻结OOS回测结果，只用于解释当前版本；不得据此在同一测试窗内"
            "调参、筛因子或重写门槛。"
        ),
    }


def dataset_feature_redundancy(
    dataset_hash: str,
    artifact_root: str | Path,
    *,
    threshold: float = 0.85,
) -> dict[str, Any]:
    """Compute train-only daily cross-sectional factor correlation groups."""
    clean_hash = str(dataset_hash or "").strip().lower()
    if not _DATASET_HASH.fullmatch(clean_hash):
        raise ValueError("dataset_hash必须是64位十六进制摘要")
    clean_threshold = round(float(threshold), 4)
    if clean_threshold < 0.5 or clean_threshold > 0.99:
        raise ValueError("相关性阈值必须在0.50到0.99之间")
    root = Path(artifact_root).resolve()
    return _cached_dataset_feature_redundancy(
        clean_hash, root.as_posix(), clean_threshold,
    )


def dataset_factor_validation_audit(
    dataset_hash: str,
    artifact_root: str | Path,
) -> dict[str, Any]:
    """Audit frozen factors with train and validation data only.

    This diagnostic is intended for feature selection between immutable model
    versions. It deliberately omits every test-period statistic so researchers
    cannot accidentally tune a new candidate against the held-out segment.
    """
    clean_hash = str(dataset_hash or "").strip().lower()
    if not _DATASET_HASH.fullmatch(clean_hash):
        raise ValueError("dataset_hash必须是64位十六进制摘要")
    root = Path(artifact_root).resolve()
    return _cached_dataset_factor_validation_audit(clean_hash, root.as_posix())


@lru_cache(maxsize=64)
def _cached_dataset_factor_validation_audit(
    dataset_hash: str,
    artifact_root: str,
) -> dict[str, Any]:
    dataset_dir = Path(artifact_root) / "datasets" / dataset_hash
    manifest_path = dataset_dir / "dataset_manifest.json"
    raw_path = dataset_dir / "dataset_raw.parquet"
    if not manifest_path.is_file() or not raw_path.is_file():
        raise FileNotFoundError("冻结数据集快照不完整，无法进行因子验证审计")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("冻结数据集manifest无法读取") from exc
    if str(manifest.get("dataset_spec_hash") or "") != dataset_hash:
        raise ValueError("冻结数据集manifest与dataset_hash不一致")
    feature_names = [str(item) for item in manifest.get("feature_names") or []]
    if not feature_names:
        raise ValueError("冻结数据集没有可审计特征")
    if len(feature_names) > 120:
        raise ValueError("单次因子验证审计最多支持120个冻结特征")
    segments = dict(manifest.get("segments") or {})
    train_range = _segment_range(segments.get("train"), "train")
    valid_range = _segment_range(segments.get("valid"), "valid")
    frame = pd.read_parquet(raw_path)
    feature_columns = [("feature", name) for name in feature_names]
    missing = [
        name for name, column in zip(feature_names, feature_columns)
        if column not in frame
    ]
    if missing or ("label", "LABEL0") not in frame:
        raise ValueError(
            "冻结数据集缺少因子验证审计列: "
            + ", ".join(missing or ["LABEL0"])
        )
    dates = pd.to_datetime(
        frame.index.get_level_values("datetime"), errors="coerce",
    )
    split_frames: dict[str, pd.DataFrame] = {}
    for split, date_range in (("train", train_range), ("valid", valid_range)):
        mask = (dates >= date_range[0]) & (dates <= date_range[1])
        selected = frame.loc[
            mask, feature_columns + [("label", "LABEL0")],
        ].copy()
        selected.columns = feature_names + ["__label__"]
        split_frames[split] = selected
    split_metrics = {
        split: _factor_rank_ic_metrics(source, feature_names)
        for split, source in split_frames.items()
    }
    rows = []
    status_counts = {
        "stable": 0, "improved": 0, "decayed": 0,
        "reversed": 0, "weak": 0,
    }
    recommendation_counts = {
        "keep_candidate": 0, "observe": 0, "review": 0,
    }
    coverage = dict(manifest.get("coverage") or {})
    for feature in feature_names:
        train = split_metrics["train"][feature]
        valid = split_metrics["valid"][feature]
        train_ic = float(train["rank_ic"])
        valid_ic = float(valid["rank_ic"])
        train_strength = abs(train_ic)
        valid_strength = abs(valid_ic)
        same_direction = bool(train_ic * valid_ic >= 0)
        retention = valid_strength / train_strength if train_strength > 1e-12 else None
        if valid_strength < 0.005:
            status = "weak"
        elif not same_direction:
            status = "reversed"
        elif valid_strength >= train_strength * 1.10:
            status = "improved"
        elif valid_strength >= train_strength * 0.50:
            status = "stable"
        else:
            status = "decayed"
        if status in {"stable", "improved"} and valid_strength >= 0.02:
            recommendation = "keep_candidate"
        elif status in {"reversed", "weak"}:
            recommendation = "review"
        else:
            recommendation = "observe"
        status_counts[status] += 1
        recommendation_counts[recommendation] += 1
        factor_id = feature.split("__v", 1)[0]
        rows.append({
            "factor": feature,
            "factor_id": factor_id,
            "coverage": _optional_float(coverage.get(factor_id)),
            "train": train,
            "valid": valid,
            "same_direction": same_direction,
            "absolute_ic_retention": retention,
            "status": status,
            "recommendation": recommendation,
        })
    rows.sort(key=lambda item: (
        -abs(float(item["valid"]["ic_ir"])),
        -abs(float(item["valid"]["rank_ic"])),
        item["factor"],
    ))
    stable_count = status_counts["stable"] + status_counts["improved"]
    conclusion = (
        f"{stable_count}/{len(rows)}个因子在训练与验证段方向一致且信号保持；"
        f"{recommendation_counts['review']}个因子建议进入下一冻结版本复核。"
    )
    return {
        "dataset_hash": dataset_hash,
        "feature_count": len(feature_names),
        "train_range": [
            train_range[0].date().isoformat(), train_range[1].date().isoformat(),
        ],
        "valid_range": [
            valid_range[0].date().isoformat(), valid_range[1].date().isoformat(),
        ],
        "train_rows": int(len(split_frames["train"])),
        "valid_rows": int(len(split_frames["valid"])),
        "status_counts": status_counts,
        "recommendation_counts": recommendation_counts,
        "factors": rows,
        "conclusion": conclusion,
        "method": {
            "kind": "daily_cross_sectional_spearman",
            "minimum_stocks_per_day": 30,
            "selection_scope": "train_and_validation_only",
            "test_segment_read": False,
            "guard": (
                "只允许用训练段与验证段生成下一冻结版本候选；"
                "不读取测试段、WFA测试窗或回测收益。"
            ),
        },
    }


def _factor_rank_ic_metrics(
    frame: pd.DataFrame,
    feature_names: list[str],
) -> dict[str, dict[str, Any]]:
    daily_ics: dict[str, list[float]] = {name: [] for name in feature_names}
    for _, group in frame.groupby(level="datetime", sort=True):
        if len(group) < 30:
            continue
        daily = group[feature_names + ["__label__"]].corr(
            method="spearman", min_periods=30,
        )
        for name in feature_names:
            value = daily.loc[name, "__label__"]
            if pd.notna(value):
                daily_ics[name].append(float(value))
    result = {}
    for name in feature_names:
        values = np.asarray(daily_ics[name], dtype=float)
        mean = float(values.mean()) if values.size else 0.0
        std = float(values.std(ddof=1)) if values.size > 1 else 0.0
        result[name] = {
            "rank_ic": mean,
            "ic_ir": mean / (std + 1e-12),
            "positive_rate": float(np.mean(values > 0)) if values.size else 0.0,
            "days": int(values.size),
        }
    return result


@lru_cache(maxsize=64)
def _cached_dataset_feature_redundancy(
    dataset_hash: str,
    artifact_root: str,
    threshold: float,
) -> dict[str, Any]:
    dataset_dir = Path(artifact_root) / "datasets" / dataset_hash
    manifest_path = dataset_dir / "dataset_manifest.json"
    raw_path = dataset_dir / "dataset_raw.parquet"
    if not manifest_path.is_file() or not raw_path.is_file():
        raise FileNotFoundError("冻结数据集快照不完整，无法进行特征冗余诊断")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("冻结数据集manifest无法读取") from exc
    if str(manifest.get("dataset_spec_hash") or "") != dataset_hash:
        raise ValueError("冻结数据集manifest与dataset_hash不一致")
    feature_names = [str(item) for item in manifest.get("feature_names") or []]
    if not feature_names:
        raise ValueError("冻结数据集没有可诊断特征")
    if len(feature_names) > 120:
        raise ValueError("单次特征冗余诊断最多支持120个冻结特征")
    train_range = _segment_range(
        dict(manifest.get("segments") or {}).get("train"), "train",
    )
    frame = pd.read_parquet(raw_path)
    required_columns = [("feature", name) for name in feature_names]
    missing = [name for name, column in zip(feature_names, required_columns) if column not in frame]
    if missing or ("label", "LABEL0") not in frame:
        raise ValueError("冻结数据集缺少冗余诊断列: " + ", ".join(missing or ["LABEL0"]))
    dates = pd.to_datetime(frame.index.get_level_values("datetime"), errors="coerce")
    train_mask = (dates >= train_range[0]) & (dates <= train_range[1])
    train = frame.loc[train_mask, required_columns + [("label", "LABEL0")]].copy()
    train.columns = feature_names + ["__label__"]
    feature_count = len(feature_names)
    corr_sums = np.zeros((feature_count, feature_count), dtype=float)
    corr_counts = np.zeros((feature_count, feature_count), dtype=int)
    daily_ics: dict[str, list[float]] = {name: [] for name in feature_names}
    usable_dates = 0
    for _, group in train.groupby(level="datetime", sort=True):
        if len(group) < 30:
            continue
        daily = group.corr(method="spearman", min_periods=30)
        feature_corr = daily.loc[feature_names, feature_names].to_numpy(dtype=float)
        finite = np.isfinite(feature_corr)
        corr_sums[finite] += feature_corr[finite]
        corr_counts[finite] += 1
        for name in feature_names:
            value = daily.loc[name, "__label__"]
            if pd.notna(value):
                daily_ics[name].append(float(value))
        usable_dates += 1
    if usable_dates == 0:
        raise ValueError("训练段没有足够股票计算截面相关性")
    correlation = np.divide(
        corr_sums,
        corr_counts,
        out=np.full_like(corr_sums, np.nan),
        where=corr_counts > 0,
    )
    factor_metrics = []
    metric_map: dict[str, dict[str, Any]] = {}
    for name in feature_names:
        values = np.asarray(daily_ics[name], dtype=float)
        mean = float(values.mean()) if values.size else 0.0
        std = float(values.std(ddof=1)) if values.size > 1 else 0.0
        item = {
            "factor": name,
            "train_rank_ic": mean,
            "train_ic_ir": mean / (std + 1e-12),
            "positive_rate": float(np.mean(values > 0)) if values.size else 0.0,
            "days": int(values.size),
        }
        factor_metrics.append(item)
        metric_map[name] = item
    pairs = []
    adjacency: dict[str, set[str]] = {name: set() for name in feature_names}
    off_diagonal = []
    for left in range(feature_count):
        for right in range(left + 1, feature_count):
            value = correlation[left, right]
            if not np.isfinite(value):
                continue
            absolute = abs(float(value))
            off_diagonal.append(absolute)
            if absolute >= threshold:
                left_name, right_name = feature_names[left], feature_names[right]
                pairs.append({
                    "left": left_name,
                    "right": right_name,
                    "correlation": float(value),
                    "absolute_correlation": absolute,
                    "days": int(corr_counts[left, right]),
                })
                adjacency[left_name].add(right_name)
                adjacency[right_name].add(left_name)
    pairs.sort(key=lambda item: (-item["absolute_correlation"], item["left"], item["right"]))
    groups = []
    visited: set[str] = set()
    for feature in feature_names:
        if feature in visited or not adjacency[feature]:
            continue
        pending = [feature]
        component = []
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            component.append(current)
            pending.extend(sorted(adjacency[current] - visited, reverse=True))
        component.sort()
        keep = max(
            component,
            key=lambda name: (
                abs(float(metric_map[name]["train_ic_ir"])),
                abs(float(metric_map[name]["train_rank_ic"])),
                name,
            ),
        )
        groups.append({
            "group": len(groups) + 1,
            "features": component,
            "recommended_keep": keep,
            "review_candidates": [name for name in component if name != keep],
            "keep_basis": "训练段|ICIR|优先，其次|RankIC|；仅用于新冻结实验候选",
            "max_absolute_correlation": max(
                item["absolute_correlation"] for item in pairs
                if item["left"] in component and item["right"] in component
            ),
        })
    redundant_count = sum(len(item["review_candidates"]) for item in groups)
    matrix = [
        [float(value) if np.isfinite(value) else None for value in row]
        for row in correlation
    ]
    max_correlation = max(off_diagonal, default=0.0)
    mean_correlation = float(np.mean(off_diagonal)) if off_diagonal else 0.0
    conclusion = (
        f"发现{len(groups)}组高相关特征，{redundant_count}个因子可进入新实验复核。"
        if groups else "训练段未发现超过阈值的高相关特征。"
    )
    return {
        "dataset_hash": dataset_hash,
        "threshold": threshold,
        "train_range": [train_range[0].date().isoformat(), train_range[1].date().isoformat()],
        "train_rows": int(len(train)),
        "train_days": usable_dates,
        "feature_count": feature_count,
        "high_correlation_pair_count": len(pairs),
        "redundancy_group_count": len(groups),
        "review_candidate_count": redundant_count,
        "max_absolute_correlation": max_correlation,
        "mean_absolute_correlation": mean_correlation,
        "conclusion": conclusion,
        "features": feature_names,
        "matrix": matrix,
        "factor_metrics": factor_metrics,
        "pairs": pairs,
        "groups": groups,
        "method": {
            "kind": "mean_daily_cross_sectional_spearman",
            "minimum_stocks_per_day": 30,
            "selection_scope": "train_only",
            "guard": "不使用验证段、测试段或回测收益决定保留因子",
        },
    }


def artifact_model_feature_importance(
    bundle_path: str | Path,
    feature_names: list[str],
) -> list[dict[str, Any]]:
    """Recover importance from a trusted, hash-verified internal model bundle."""
    path = Path(bundle_path).resolve()
    if not path.is_file():
        raise FileNotFoundError("模型bundle不存在")
    return _cached_artifact_model_feature_importance(
        path.as_posix(), tuple(str(item) for item in feature_names),
    )


def artifact_model_shap_summary(
    bundle_path: str | Path,
    dataset_path: str | Path,
    *,
    model_kind: str,
    segments: dict[str, Any],
    feature_names: list[str],
    split: str = "valid",
    sample_rows: int = 30_000,
) -> dict[str, Any]:
    """Explain a frozen tree model with deterministic native SHAP values."""
    bundle = Path(bundle_path).resolve()
    dataset = Path(dataset_path).resolve()
    if not bundle.is_file():
        raise FileNotFoundError("模型bundle不存在")
    if not dataset.is_file():
        raise FileNotFoundError("冻结dataset.parquet不存在")
    clean_kind = str(model_kind or "").strip().lower()
    if clean_kind not in {"lightgbm", "xgboost", "catboost"}:
        raise ValueError("SHAP归因当前仅支持LightGBM、XGBoost和CatBoost")
    clean_split = str(split or "valid").strip().lower()
    if clean_split not in {"train", "valid", "test"}:
        raise ValueError("SHAP数据分段必须是train、valid或test")
    clean_rows = int(sample_rows)
    if clean_rows < 1 or clean_rows > 100_000:
        raise ValueError("SHAP样本数必须在1到100000之间")
    clean_segments = tuple(
        (str(name), str(value[0]), str(value[1]))
        for name, value in sorted(segments.items())
        if isinstance(value, (list, tuple)) and len(value) == 2
    )
    return _cached_artifact_model_shap_summary(
        bundle.as_posix(), dataset.as_posix(), clean_kind, clean_segments,
        tuple(str(item) for item in feature_names), clean_split, clean_rows,
    )


def artifact_model_training_diagnostics(
    bundle_path: str | Path,
    *,
    model_kind: str,
    model_params: dict[str, Any],
) -> dict[str, Any]:
    """Read normalized training history, recovering old runs from MLflow SQLite."""
    path = Path(bundle_path).resolve()
    if not path.is_file():
        raise FileNotFoundError("模型bundle不存在")
    params_json = json.dumps(model_params or {}, sort_keys=True, separators=(",", ":"))
    return _cached_artifact_model_training_diagnostics(
        path.as_posix(), str(model_kind or ""), params_json,
    )


@lru_cache(maxsize=64)
def _cached_artifact_model_feature_importance(
    bundle_path: str,
    feature_names: tuple[str, ...],
) -> list[dict[str, Any]]:
    from factor_service.research.trainer import _feature_importance

    model = _load_artifact_model(bundle_path)
    return _feature_importance(model, list(feature_names))


@lru_cache(maxsize=32)
def _cached_artifact_model_shap_summary(
    bundle_path: str,
    dataset_path: str,
    model_kind: str,
    segments: tuple[tuple[str, str, str], ...],
    feature_names: tuple[str, ...],
    split: str,
    sample_rows: int,
) -> dict[str, Any]:
    segment_map = {name: (start, end) for name, start, end in segments}
    if split not in segment_map:
        raise ValueError(f"冻结数据集缺少{split}时间分段")
    frame = pd.read_parquet(dataset_path)
    feature_columns = [("feature", name) for name in feature_names]
    missing = [
        name for name, column in zip(feature_names, feature_columns)
        if column not in frame
    ]
    if missing:
        raise ValueError("冻结数据集缺少SHAP解释特征: " + ", ".join(missing))
    start, end = _segment_range(segment_map[split], split)
    dates = pd.to_datetime(frame.index.get_level_values("datetime"), errors="coerce")
    mask = (dates >= start) & (dates <= end)
    features = frame.loc[mask, feature_columns].copy()
    features.columns = list(feature_names)
    features = features.apply(pd.to_numeric, errors="coerce").replace(
        [np.inf, -np.inf], np.nan,
    )
    available_rows = int(len(features))
    if available_rows == 0:
        raise ValueError(f"冻结数据集{split}分段没有可解释样本")
    if available_rows > sample_rows:
        features = features.sample(sample_rows, random_state=42)
    model = _load_artifact_model(bundle_path)
    raw_model = getattr(model, "model", None)
    if raw_model is None:
        raise ValueError("模型bundle缺少已训练树模型")
    matrix = features.to_numpy(dtype=np.float32)
    if model_kind == "lightgbm":
        best_iteration = getattr(raw_model, "best_iteration", None) or None
        contributions = raw_model.predict(
            matrix, num_iteration=best_iteration, pred_contrib=True,
        )
        predictions = raw_model.predict(matrix, num_iteration=best_iteration)
    elif model_kind == "xgboost":
        import xgboost as xgb

        dmatrix = xgb.DMatrix(matrix)
        contributions = raw_model.predict(dmatrix, pred_contribs=True)
        predictions = raw_model.predict(dmatrix)
    else:
        from catboost import Pool

        pool = Pool(matrix)
        contributions = raw_model.get_feature_importance(
            pool, type="ShapValues",
        )
        predictions = raw_model.predict(pool)
    contribution_matrix = np.asarray(contributions, dtype=float)
    if (
        contribution_matrix.ndim != 2
        or contribution_matrix.shape[1] < len(feature_names) + 1
    ):
        raise ValueError(
            "SHAP贡献列与冻结特征不一致: "
            f"{getattr(contribution_matrix, 'shape', None)}"
        )
    shap_values = contribution_matrix[:, :len(feature_names)]
    base_values = contribution_matrix[:, -1]
    predictions = np.asarray(predictions, dtype=float).reshape(-1)
    reconstructed = shap_values.sum(axis=1) + base_values
    local_errors = np.abs(reconstructed - predictions)
    mean_abs_values = np.mean(np.abs(shap_values), axis=0)
    total_mean_abs = float(mean_abs_values.sum())
    rows = []
    for index, name in enumerate(feature_names):
        mean_abs = float(mean_abs_values[index])
        rows.append({
            "factor": name,
            "mean_abs_shap": mean_abs,
            "mean_shap": float(np.mean(shap_values[:, index])),
            "positive_ratio": float(np.mean(shap_values[:, index] > 0)),
            "contribution_share": mean_abs / total_mean_abs if total_mean_abs else 0.0,
        })
    rows.sort(key=lambda item: (-item["mean_abs_shap"], item["factor"]))
    cumulative = 0.0
    for rank, item in enumerate(rows, start=1):
        cumulative += float(item["contribution_share"])
        item["rank"] = rank
        item["cumulative_share"] = cumulative
    return {
        "model_kind": model_kind,
        "split": split,
        "segment_range": [start.date().isoformat(), end.date().isoformat()],
        "rows_available": available_rows,
        "rows_used": int(len(features)),
        "feature_count": len(feature_names),
        "base_value_mean": float(np.mean(base_values)),
        "local_accuracy_mean_abs_error": float(np.mean(local_errors)),
        "local_accuracy_max_abs_error": float(np.max(local_errors)),
        "features": rows,
        "method": {
            "kind": "native_tree_shap",
            "sample_seed": 42,
            "model_frozen": True,
            "labels_used": False,
            "guard": "默认解释验证段；归因不重新训练模型，也不作为测试段调参依据",
        },
    }


@lru_cache(maxsize=64)
def _cached_artifact_model_training_diagnostics(
    bundle_path: str,
    model_kind: str,
    model_params_json: str,
) -> dict[str, Any]:
    from factor_service.research.training_diagnostics import (
        build_training_diagnostics,
        build_training_diagnostics_from_series,
    )

    model_params = json.loads(model_params_json)
    with tarfile.open(bundle_path, "r:gz") as archive:
        for member_name in ("training_diagnostics.json", "manifest.json"):
            try:
                source = archive.extractfile(member_name)
            except KeyError:
                source = None
            if source is None:
                continue
            try:
                payload = json.loads(source.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            diagnostics = (
                payload.get("training_diagnostics")
                if member_name == "manifest.json" and isinstance(payload, dict)
                else payload
            )
            if isinstance(diagnostics, dict) and "available" in diagnostics:
                return diagnostics
        try:
            database_member = archive.getmember("mlflow.db")
            database_source = archive.extractfile(database_member)
        except KeyError:
            database_source = None
        if database_source is None:
            return build_training_diagnostics(model_kind, {}, model_params)
        database_bytes = database_source.read()
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(database_bytes)
        rows = connection.execute(
            "SELECT key, step, value, timestamp FROM metrics "
            "ORDER BY key, step, timestamp",
        ).fetchall()
    except (sqlite3.Error, AttributeError):
        return build_training_diagnostics(model_kind, {}, model_params)
    finally:
        connection.close()
    by_key: dict[str, dict[int, tuple[int, float]]] = {}
    for key, step, value, timestamp in rows:
        numeric = float(value)
        if not np.isfinite(numeric):
            continue
        by_key.setdefault(str(key), {})[int(step)] = (int(timestamp), numeric)
    candidates = []
    for train_key in by_key:
        if train_key == "train_loss":
            valid_key = "valid_loss"
        elif train_key.endswith(".train"):
            valid_key = train_key[:-len(".train")] + ".valid"
        else:
            continue
        if valid_key not in by_key:
            continue
        shared_steps = sorted(set(by_key[train_key]) & set(by_key[valid_key]))
        if not shared_steps:
            continue
        priority = 2 if train_key.startswith("final.") else 1
        candidates.append((priority, len(shared_steps), train_key, valid_key, shared_steps))
    if not candidates:
        return build_training_diagnostics(model_kind, {}, model_params)
    _, _, train_key, valid_key, raw_steps = max(candidates)
    if train_key == "train_loss":
        interval = int(model_params.get("eval_steps") or 1)
        steps = [max(1, step) * interval for step in raw_steps]
        metric_name = "loss"
    else:
        steps = [step + 1 for step in raw_steps]
        metric_name = train_key.removeprefix("final.").removesuffix(".train")
    return build_training_diagnostics_from_series(
        model_kind=model_kind,
        metric_name=metric_name,
        train_values=[by_key[train_key][step][1] for step in raw_steps],
        valid_values=[by_key[valid_key][step][1] for step in raw_steps],
        steps=steps,
        model_params=model_params,
    )


def artifact_model_permutation_importance(
    bundle_path: str | Path,
    dataset_path: str | Path,
    *,
    model_kind: str,
    segments: dict[str, Any],
    model_params: dict[str, Any],
    feature_names: list[str],
) -> dict[str, Any]:
    """Measure held-out contribution by PIT-safe within-date permutation.

    The model is never refitted. Each feature is shuffled only among stocks on
    the same date, so no value is borrowed from a later date. For sequence
    models the complete frozen history remains available to TSDatasetH.
    """
    bundle = Path(bundle_path).resolve()
    dataset = Path(dataset_path).resolve()
    if not bundle.is_file():
        raise FileNotFoundError("模型bundle不存在")
    if not dataset.is_file():
        raise FileNotFoundError("冻结dataset.parquet不存在")
    clean_segments = tuple(
        (str(name), str(value[0]), str(value[1]))
        for name, value in sorted(segments.items())
        if isinstance(value, (list, tuple)) and len(value) == 2
    )
    clean_params = json.dumps(model_params or {}, sort_keys=True, separators=(",", ":"))
    return _cached_artifact_model_permutation_importance(
        bundle.as_posix(), dataset.as_posix(), str(model_kind), clean_segments,
        clean_params, tuple(str(item) for item in feature_names),
    )


def isolated_artifact_model_permutation_importance(
    bundle_path: str | Path,
    dataset_path: str | Path,
    *,
    model_kind: str,
    segments: dict[str, Any],
    model_params: dict[str, Any],
    feature_names: list[str],
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Run memory-heavy diagnostics in a short-lived Python process."""
    payload = {
        "bundle_path": str(Path(bundle_path).resolve()),
        "dataset_path": str(Path(dataset_path).resolve()),
        "model_kind": str(model_kind),
        "segments": segments,
        "model_params": model_params,
        "feature_names": feature_names,
    }
    completed = subprocess.run(
        [sys.executable, "-m", "factor_service.model_diagnostics_cli"],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=max(30, int(timeout_seconds)),
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "未知错误"
        raise RuntimeError(f"隔离模型解释失败: {detail}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("隔离模型解释返回了无效结果") from exc
    if not isinstance(result, dict):
        raise RuntimeError("隔离模型解释结果格式无效")
    return result


@lru_cache(maxsize=32)
def _cached_artifact_model_permutation_importance(
    bundle_path: str,
    dataset_path: str,
    model_kind: str,
    segments: tuple[tuple[str, str, str], ...],
    model_params_json: str,
    feature_names: tuple[str, ...],
) -> dict[str, Any]:
    from qlib.data.dataset import DataHandlerLP, DatasetH

    from factor_service.research.trainer import (
        QlibStackingModel,
        SEQUENCE_MODEL_KINDS,
        _dataset_for_model,
        _metrics,
        _predict_dataset,
    )

    segment_map = {name: (start, end) for name, start, end in segments}
    if "test" not in segment_map:
        raise ValueError("冻结数据集缺少test时间分段")
    model_params = json.loads(model_params_json)
    frame = pd.read_parquet(dataset_path)
    missing = [name for name in feature_names if ("feature", name) not in frame.columns]
    if missing:
        raise ValueError("冻结数据集缺少解释特征: " + ", ".join(missing))
    evaluation_segments = dict(segment_map)
    sampled_test_days = None
    stacking_base_specs = (
        list(model_params.get("base_models") or [])
        if model_kind == "stacking" else []
    )
    contains_sequence_model = model_kind in SEQUENCE_MODEL_KINDS or any(
        str(item.get("kind") or "") in SEQUENCE_MODEL_KINDS
        for item in stacking_base_specs
        if isinstance(item, dict)
    )
    if contains_sequence_model:
        import torch

        torch.set_num_threads(1)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            pass
        torch.use_deterministic_algorithms(True)
        test_start, test_end = segment_map["test"]
        dates = pd.Index(frame.index.get_level_values("datetime")).unique().sort_values()
        test_dates = dates[
            (dates >= pd.Timestamp(test_start)) & (dates <= pd.Timestamp(test_end))
        ]
        if len(test_dates) > 10:
            test_dates = test_dates[-10:]
            evaluation_segments["test"] = (
                pd.Timestamp(test_dates[0]).date().isoformat(),
                pd.Timestamp(test_dates[-1]).date().isoformat(),
            )
            sampled_test_days = 10
    model = _load_artifact_model(bundle_path)
    if model_kind == "stacking" and not isinstance(model, QlibStackingModel):
        raise ValueError("Stacking模型bundle类型无效")

    def predict(target_frame: pd.DataFrame) -> pd.Series:
        handler = DataHandlerLP.from_df(target_frame)
        if model_kind != "stacking":
            dataset = _dataset_for_model(
                handler, evaluation_segments, model_kind, model_params, DatasetH,
            )
            return _predict_dataset(
                model, model_kind, dataset, "test",
                classification=bool(getattr(model, "classification", False)),
            )
        predictions: list[pd.Series] = []
        for item in model.base_models:
            kind = str(item.get("kind") or "")
            params = dict(item.get("params") or {})
            dataset = _dataset_for_model(
                handler, evaluation_segments, kind, params, DatasetH,
            )
            predictions.append(_predict_dataset(
                item.get("model"), kind, dataset, "test",
                classification=model.classification,
            ).rename(kind))
        aligned = pd.concat(predictions, axis=1, join="inner").dropna()
        if aligned.empty:
            raise ValueError("Stacking基模型诊断预测没有共同有效样本")
        values = model.combine([
            aligned.iloc[:, index].to_numpy(dtype=float)
            for index in range(len(predictions))
        ])
        return pd.Series(values, index=aligned.index, name="prediction")

    baseline_prediction = predict(frame)
    baseline = _metrics(baseline_prediction, frame, evaluation_segments["test"])
    del baseline_prediction
    gc.collect()
    rows = []
    for index, feature in enumerate(feature_names):
        shuffled = frame.copy()
        values = shuffled[("feature", feature)].copy()
        rng = np.random.default_rng(_stable_seed(feature, index))
        for positions in values.groupby(level="datetime", sort=False).indices.values():
            permuted = values.iloc[positions].to_numpy(copy=True)
            rng.shuffle(permuted)
            values.iloc[positions] = permuted
        shuffled[("feature", feature)] = values
        prediction = predict(shuffled)
        metrics = _metrics(prediction, shuffled, evaluation_segments["test"])
        rows.append({
            "factor": feature,
            "permutation_rmse": metrics["rmse"],
            "delta_rmse": float(metrics["rmse"] - baseline["rmse"]),
            "permutation_rank_ic": metrics["rank_ic"],
            "rank_ic_drop": float(baseline["rank_ic"] - metrics["rank_ic"]),
            "permutation_ic_ir": metrics["ic_ir"],
        })
        del prediction, shuffled, values
        gc.collect()
    rows.sort(key=lambda item: (-float(item["rank_ic_drop"]), item["factor"]))
    positive = sum(float(item["rank_ic_drop"]) > 0 for item in rows)
    negative = sum(float(item["rank_ic_drop"]) < 0 for item in rows)
    return {
        "baseline": {
            "rmse": baseline["rmse"],
            "rank_ic": baseline["rank_ic"],
            "ic_ir": baseline["ic_ir"],
            "rows": baseline["test_rows"],
            "days": baseline["test_days"],
        },
        "features": rows,
        "feature_count": len(rows),
        "positive_contribution_count": positive,
        "non_positive_contribution_count": negative,
        "method": {
            "kind": "held_out_cross_sectional_permutation",
            "repeats": 1,
            "seed": 42,
            "scope": (
                "latest independent test window"
                if sampled_test_days else "full independent test segment"
            ),
            "sampled_test_days": sampled_test_days,
            "causal_constraint": "每个交易日内跨股票置换，不跨日期借值，模型不重新训练",
        },
    }


@lru_cache(maxsize=32)
def _load_artifact_model(bundle_path: str) -> Any:
    with tarfile.open(bundle_path, "r:gz") as archive:
        try:
            member = archive.getmember("model.pkl")
        except KeyError as exc:
            raise ValueError("模型bundle缺少model.pkl") from exc
        if member.size > 2 * 1024 * 1024 * 1024:
            raise ValueError("模型文件超过安全读取上限")
        source = archive.extractfile(member)
        if source is None:
            raise ValueError("模型bundle中的model.pkl无法读取")
        # The bundle is produced by our isolated trainer and SHA256-verified on
        # publication; user uploads never reach this path.
        return pickle.loads(source.read())


def _stable_seed(feature: str, index: int) -> int:
    payload = f"42:{index}:{feature}".encode("utf-8")
    return int.from_bytes(__import__("hashlib").sha256(payload).digest()[:8], "big")


@lru_cache(maxsize=64)
def _cached_dataset_feature_drift(
    dataset_hash: str,
    artifact_root: str,
) -> dict[str, Any]:
    dataset_dir = Path(artifact_root) / "datasets" / dataset_hash
    manifest_path = dataset_dir / "dataset_manifest.json"
    raw_path = dataset_dir / "dataset_raw.parquet"
    if not manifest_path.is_file() or not raw_path.is_file():
        raise FileNotFoundError("冻结数据集快照不完整，无法进行特征漂移诊断")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("冻结数据集manifest无法读取") from exc
    if str(manifest.get("dataset_spec_hash") or "") != dataset_hash:
        raise ValueError("冻结数据集manifest与dataset_hash不一致")
    segments = dict(manifest.get("segments") or {})
    train_range = _segment_range(segments.get("train"), "train")
    test_range = _segment_range(segments.get("test"), "test")
    feature_names = [str(item) for item in manifest.get("feature_names") or []]
    if not feature_names:
        raise ValueError("冻结数据集没有可诊断特征")

    frame = pd.read_parquet(raw_path)
    missing_columns = [name for name in feature_names if ("feature", name) not in frame.columns]
    if missing_columns:
        raise ValueError("冻结数据集缺少特征列: " + ", ".join(missing_columns))
    dates = pd.to_datetime(frame.index.get_level_values("datetime"), errors="coerce")
    train_mask = (dates >= train_range[0]) & (dates <= train_range[1])
    test_mask = (dates >= test_range[0]) & (dates <= test_range[1])
    train_rows = int(train_mask.sum())
    test_rows = int(test_mask.sum())
    if train_rows == 0 or test_rows == 0:
        raise ValueError("冻结数据集训练段或测试段为空")

    rows = []
    for name in feature_names:
        values = pd.to_numeric(frame[("feature", name)], errors="coerce")
        train = values.loc[train_mask]
        test = values.loc[test_mask]
        train_finite = train[np.isfinite(train.to_numpy(dtype=float, na_value=np.nan))]
        test_finite = test[np.isfinite(test.to_numpy(dtype=float, na_value=np.nan))]
        psi = _population_stability_index(train_finite, test_finite)
        ks = _ks_statistic(train_finite, test_finite)
        status = _drift_status(psi)
        train_missing = float(train.isna().mean())
        test_missing = float(test.isna().mean())
        rows.append({
            "factor": name,
            "status": status,
            "psi": psi,
            "ks": ks,
            "train_count": int(train_finite.shape[0]),
            "test_count": int(test_finite.shape[0]),
            "train_missing_ratio": train_missing,
            "test_missing_ratio": test_missing,
            "missing_ratio_delta": test_missing - train_missing,
            "train_mean": _finite_stat(train_finite, "mean"),
            "test_mean": _finite_stat(test_finite, "mean"),
            "train_std": _finite_stat(train_finite, "std"),
            "test_std": _finite_stat(test_finite, "std"),
            "train_median": _finite_stat(train_finite, "median"),
            "test_median": _finite_stat(test_finite, "median"),
        })
    rows.sort(key=lambda item: (-float(item["psi"] or 0.0), item["factor"]))
    counts = {
        level: sum(item["status"] == level for item in rows)
        for level in ("stable", "medium", "severe")
    }
    psi_values = [float(item["psi"]) for item in rows if item["psi"] is not None]
    max_psi = max(psi_values, default=None)
    mean_psi = float(np.mean(psi_values)) if psi_values else None
    if counts["severe"]:
        conclusion = f"{counts['severe']}个特征出现显著漂移，需检查模型在最新样本外区间的稳定性。"
        status = "severe"
    elif counts["medium"]:
        conclusion = f"{counts['medium']}个特征出现中等漂移，建议结合RankIC和推理稳定性继续观察。"
        status = "medium"
    else:
        conclusion = "训练段与样本外测试段的特征分布整体稳定。"
        status = "stable"
    return {
        "dataset_hash": dataset_hash,
        "status": status,
        "conclusion": conclusion,
        "train_range": [train_range[0].date().isoformat(), train_range[1].date().isoformat()],
        "test_range": [test_range[0].date().isoformat(), test_range[1].date().isoformat()],
        "train_rows": train_rows,
        "test_rows": test_rows,
        "feature_count": len(rows),
        "max_psi": max_psi,
        "mean_psi": mean_psi,
        "counts": counts,
        "features": rows,
        "method": {
            "psi_bins": 10,
            "stable": "PSI < 0.10",
            "medium": "0.10 ≤ PSI < 0.25",
            "severe": "PSI ≥ 0.25",
            "source": "冻结dataset_raw.parquet；训练段对独立测试段",
        },
    }


@lru_cache(maxsize=64)
def _cached_dataset_walk_forward_attribution(
    dataset_hash: str,
    artifact_root: str,
    windows_json: str,
) -> dict[str, Any]:
    dataset_dir = Path(artifact_root) / "datasets" / dataset_hash
    manifest_path = dataset_dir / "dataset_manifest.json"
    raw_path = dataset_dir / "dataset_raw.parquet"
    if not manifest_path.is_file() or not raw_path.is_file():
        raise FileNotFoundError("冻结数据集快照不完整，无法进行WFA窗口归因")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        windows = json.loads(windows_json)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("冻结数据集或WFA窗口定义无法读取") from exc
    if str(manifest.get("dataset_spec_hash") or "") != dataset_hash:
        raise ValueError("冻结数据集manifest与dataset_hash不一致")
    feature_names = [str(item) for item in manifest.get("feature_names") or []]
    if not feature_names:
        raise ValueError("冻结数据集没有可归因特征")

    frame = pd.read_parquet(raw_path)
    required = [("feature", name) for name in feature_names]
    missing = [
        name for name, column in zip(feature_names, required)
        if column not in frame.columns
    ]
    if missing or ("label", "LABEL0") not in frame.columns:
        raise ValueError("冻结数据集缺少WFA归因列: " + ", ".join(
            missing or ["LABEL0"],
        ))
    analysis = frame.loc[:, required + [("label", "LABEL0")]].copy()
    analysis.columns = feature_names + ["__label__"]
    dates = pd.to_datetime(
        analysis.index.get_level_values("datetime"), errors="coerce",
    )
    entity_counts = analysis["__label__"].notna().groupby(
        analysis.index.get_level_values("datetime"),
    ).sum()
    median_entities = float(entity_counts.median()) if len(entity_counts) else 0.0
    minimum_entities = max(2, min(30, int(round(median_entities * 0.5))))

    output_windows = []
    for frozen in windows:
        train_range = _segment_range(frozen.get("train"), "WFA train")
        test_range = _segment_range(frozen.get("test"), "WFA test")
        train_mask = (dates >= train_range[0]) & (dates <= train_range[1])
        test_mask = (dates >= test_range[0]) & (dates <= test_range[1])
        train = analysis.loc[train_mask]
        test = analysis.loc[test_mask]
        if train.empty or test.empty:
            raise ValueError(f"W{frozen['window']}训练段或测试段没有冻结样本")
        factors = []
        for name in feature_names:
            train_values = pd.to_numeric(train[name], errors="coerce")
            test_values = pd.to_numeric(test[name], errors="coerce")
            train_finite = train_values[np.isfinite(
                train_values.to_numpy(dtype=float, na_value=np.nan),
            )]
            test_finite = test_values[np.isfinite(
                test_values.to_numpy(dtype=float, na_value=np.nan),
            )]
            train_ic = _daily_rank_ic_stats(
                train, name, minimum_entities=minimum_entities,
            )
            test_ic = _daily_rank_ic_stats(
                test, name, minimum_entities=minimum_entities,
            )
            psi = _population_stability_index(train_finite, test_finite)
            ks = _ks_statistic(train_finite, test_finite)
            sign_flip = (
                abs(train_ic["rank_ic"]) >= 0.01
                and abs(test_ic["rank_ic"]) >= 0.01
                and train_ic["rank_ic"] * test_ic["rank_ic"] < 0
            )
            retention = (
                abs(test_ic["rank_ic"]) / abs(train_ic["rank_ic"])
                if abs(train_ic["rank_ic"]) >= 0.01 else None
            )
            signal_status = _factor_signal_status(
                sign_flip=sign_flip,
                retention=retention,
                drift_status=_drift_status(psi),
            )
            factors.append({
                "factor": name,
                "status": signal_status,
                "drift_status": _drift_status(psi),
                "psi": psi,
                "ks": ks,
                "train_rank_ic": train_ic["rank_ic"],
                "train_ic_ir": train_ic["ic_ir"],
                "train_positive_rate": train_ic["positive_rate"],
                "train_days": train_ic["days"],
                "test_rank_ic": test_ic["rank_ic"],
                "test_ic_ir": test_ic["ic_ir"],
                "test_positive_rate": test_ic["positive_rate"],
                "test_days": test_ic["days"],
                "rank_ic_delta": test_ic["rank_ic"] - train_ic["rank_ic"],
                "absolute_rank_ic_retention": retention,
                "sign_flip": sign_flip,
                "train_missing_ratio": float(train_values.isna().mean()),
                "test_missing_ratio": float(test_values.isna().mean()),
            })
        factors.sort(key=lambda item: (
            {"reversed": 0, "decayed": 1, "shifted": 2, "stable": 3}.get(
                str(item["status"]), 4,
            ),
            float(item["test_rank_ic"]),
        ))
        status_counts = {
            status: sum(item["status"] == status for item in factors)
            for status in ("stable", "shifted", "decayed", "reversed")
        }
        model_rank_ic = _optional_float(frozen.get("model_rank_ic"))
        output_windows.append({
            "window": int(frozen["window"]),
            "train_start": train_range[0].date().isoformat(),
            "train_end": train_range[1].date().isoformat(),
            "test_start": test_range[0].date().isoformat(),
            "test_end": test_range[1].date().isoformat(),
            "train_rows": int(len(train)),
            "test_rows": int(len(test)),
            "model_rank_ic": model_rank_ic,
            "model_ic_ir": _optional_float(frozen.get("model_ic_ir")),
            "status": _window_attribution_status(model_rank_ic, status_counts),
            "counts": status_counts,
            "features": factors,
        })
    weak_window = min(
        output_windows,
        key=lambda item: float(item["model_rank_ic"] or 0.0),
    )
    weak_counts = weak_window["counts"]
    if weak_counts["reversed"]:
        cause = "factor_sign_reversal"
        conclusion = (
            f"W{weak_window['window']}最弱，且{weak_counts['reversed']}个因子在"
            "训练段与测试段之间发生方向翻转。"
        )
    elif weak_counts["decayed"]:
        cause = "factor_signal_decay"
        conclusion = (
            f"W{weak_window['window']}最弱，{weak_counts['decayed']}个因子的"
            "截面RankIC明显衰减。"
        )
    elif weak_counts["shifted"]:
        cause = "feature_distribution_shift"
        conclusion = (
            f"W{weak_window['window']}最弱，{weak_counts['shifted']}个因子出现"
            "明显输入分布漂移。"
        )
    else:
        cause = "model_or_market_regime"
        conclusion = (
            f"W{weak_window['window']}最弱，但因子方向与分布未出现一致性异常，"
            "应继续检查模型拟合和市场状态。"
        )
    return {
        "dataset_hash": dataset_hash,
        "eligible": True,
        "policy": "alphablocks.walk-forward-factor-attribution.v1",
        "feature_count": len(feature_names),
        "window_count": len(output_windows),
        "minimum_entities_per_day": minimum_entities,
        "weak_window": weak_window,
        "primary_cause": cause,
        "conclusion": conclusion,
        "windows": output_windows,
        "method": {
            "rank_ic": "每日截面Spearman后按交易日等权平均",
            "drift": "每个WFA窗口训练段对独立测试段的PSI与KS",
            "guard": (
                "测试窗只做事后归因；当前版本不得按测试结果调参或筛因子，"
                "新实验只能使用新的冻结版本重新WFA。"
            ),
        },
    }


def _daily_rank_ic_stats(
    frame: pd.DataFrame,
    factor: str,
    *,
    minimum_entities: int,
) -> dict[str, Any]:
    values = []
    for _, group in frame.groupby(level="datetime", sort=True):
        pairs = group[[factor, "__label__"]].dropna()
        if len(pairs) < minimum_entities:
            continue
        correlation = pairs[factor].corr(pairs["__label__"], method="spearman")
        if pd.notna(correlation):
            values.append(float(correlation))
    array = np.asarray(values, dtype=float)
    mean = float(array.mean()) if array.size else 0.0
    std = float(array.std(ddof=1)) if array.size > 1 else 0.0
    return {
        "rank_ic": mean,
        "ic_ir": mean / (std + 1e-12),
        "positive_rate": float(np.mean(array > 0)) if array.size else 0.0,
        "days": int(array.size),
    }


def _factor_signal_status(
    *, sign_flip: bool, retention: float | None, drift_status: str,
) -> str:
    if sign_flip:
        return "reversed"
    if retention is not None and retention < 0.5:
        return "decayed"
    if drift_status == "severe":
        return "shifted"
    return "stable"


def _window_attribution_status(
    model_rank_ic: float | None, counts: Mapping[str, int],
) -> str:
    if model_rank_ic is not None and model_rank_ic < 0:
        return "failed"
    if counts.get("reversed", 0) or counts.get("decayed", 0):
        return "warning"
    if model_rank_ic is not None and model_rank_ic < 0.02:
        return "weak"
    return "stable"


def _json_segment(value: Any, name: str) -> list[str]:
    start, end = _segment_range(value, name)
    return [start.date().isoformat(), end.date().isoformat()]


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _difference(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def _market_regime(benchmark_annual_return: float | None) -> str:
    if benchmark_annual_return is None:
        return "unknown"
    if benchmark_annual_return >= 0.20:
        return "strong_bull"
    if benchmark_annual_return >= 0.05:
        return "bull"
    if benchmark_annual_return <= -0.10:
        return "bear"
    return "sideways"


def _segment_range(value: Any, name: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"冻结数据集缺少{name}时间分段")
    start, end = pd.Timestamp(value[0]), pd.Timestamp(value[1])
    if pd.isna(start) or pd.isna(end) or start > end:
        raise ValueError(f"冻结数据集{name}时间分段无效")
    return start, end


def _population_stability_index(
    reference: pd.Series,
    comparison: pd.Series,
    *,
    bins: int = 10,
) -> float | None:
    expected = np.asarray(reference, dtype=float)
    actual = np.asarray(comparison, dtype=float)
    expected = expected[np.isfinite(expected)]
    actual = actual[np.isfinite(actual)]
    if expected.size == 0 or actual.size == 0:
        return None
    quantiles = np.unique(np.quantile(expected, np.linspace(0, 1, bins + 1)))
    if quantiles.size == 1:
        center = float(quantiles[0])
        tolerance = max(abs(center) * 1e-9, 1e-9)
        edges = np.asarray([-np.inf, center - tolerance, center + tolerance, np.inf])
    else:
        edges = np.concatenate(([-np.inf], quantiles[1:-1], [np.inf]))
    expected_counts, _ = np.histogram(expected, bins=edges)
    actual_counts, _ = np.histogram(actual, bins=edges)
    epsilon = 1e-6
    expected_ratio = np.clip(expected_counts / expected.size, epsilon, None)
    actual_ratio = np.clip(actual_counts / actual.size, epsilon, None)
    value = np.sum((actual_ratio - expected_ratio) * np.log(actual_ratio / expected_ratio))
    return float(value)


def _ks_statistic(reference: pd.Series, comparison: pd.Series) -> float | None:
    expected = np.sort(np.asarray(reference, dtype=float))
    actual = np.sort(np.asarray(comparison, dtype=float))
    expected = expected[np.isfinite(expected)]
    actual = actual[np.isfinite(actual)]
    if expected.size == 0 or actual.size == 0:
        return None
    points = np.unique(np.concatenate((expected, actual)))
    expected_cdf = np.searchsorted(expected, points, side="right") / expected.size
    actual_cdf = np.searchsorted(actual, points, side="right") / actual.size
    return float(np.max(np.abs(expected_cdf - actual_cdf)))


def _finite_stat(values: pd.Series, statistic: str) -> float | None:
    numeric = np.asarray(values, dtype=float)
    numeric = numeric[np.isfinite(numeric)]
    if numeric.size == 0:
        return None
    if statistic == "mean":
        return float(np.mean(numeric))
    if statistic == "std":
        return float(np.std(numeric, ddof=1)) if numeric.size > 1 else 0.0
    if statistic == "median":
        return float(np.median(numeric))
    raise ValueError(f"不支持的统计量: {statistic}")


def _drift_status(psi: float | None) -> str:
    if psi is None or psi >= 0.25:
        return "severe"
    if psi >= 0.10:
        return "medium"
    return "stable"


__all__ = [
    "artifact_model_feature_importance",
    "artifact_model_shap_summary",
    "artifact_model_training_diagnostics",
    "artifact_model_permutation_importance",
    "isolated_artifact_model_permutation_importance",
    "dataset_feature_drift",
    "dataset_feature_redundancy",
    "dataset_factor_validation_audit",
    "dataset_walk_forward_attribution",
    "architecture_walk_forward_attribution",
    "_ks_statistic",
    "_population_stability_index",
]
