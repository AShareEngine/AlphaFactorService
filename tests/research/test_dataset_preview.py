from __future__ import annotations

import pandas as pd
import pytest

from factor_service.model_research_repository import ModelResearchConflict
from factor_service.research.dataset import PreparedDataset
from factor_service.research.dataset_preview import (
    _dataset_sample,
    _preview_request,
)


def _prepared() -> PreparedDataset:
    index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2024-01-02"), "000001.SZ"),
            (pd.Timestamp("2024-01-03"), "000002.SZ"),
            (pd.Timestamp("2024-01-10"), "000003.SZ"),
        ],
        names=["datetime", "instrument"],
    )
    columns = pd.MultiIndex.from_tuples([
        ("feature", "momentum"),
        ("label", "LABEL0"),
    ])
    frame = pd.DataFrame(
        [[0.1, -1.0], [0.2, 1.0], [0.3, 0.0]],
        index=index,
        columns=columns,
    )
    raw = frame.copy()
    raw[("feature", "momentum")] = [10.0, 20.0, 30.0]
    return PreparedDataset(
        frame=frame,
        raw_frame=raw,
        segments={
            "train": ("2024-01-02", "2024-01-03"),
            "valid": ("2024-01-10", "2024-01-10"),
            "test": ("2024-01-11", "2024-01-12"),
        },
        feature_names=["momentum"],
        coverage={"momentum": 1.0},
        medians={"momentum": 0.15},
        manifest={
            "universe_filter_steps": [{
                "rule_id": "listing_age",
                "before_count": 3,
                "after_count": 2,
            }],
        },
    )


def test_dataset_preview_returns_exact_processed_x_and_y() -> None:
    sample = _dataset_sample(
        _prepared(),
        {"split": "train", "rows": 1, "view": "processed"},
    )

    assert sample["row_count"] == 1
    assert sample["X"]["rows"][0]["momentum"] == 0.1
    assert sample["y"]["rows"][0]["LABEL0"] == -1.0
    assert sample["filter_steps"][0]["rule_id"] == "listing_age"


def test_dataset_preview_raw_view_uses_same_rows_before_transforms() -> None:
    sample = _dataset_sample(
        _prepared(),
        {"split": "train", "rows": 1, "view": "raw"},
    )

    assert sample["X"]["rows"][0]["momentum"] == 10.0
    assert sample["y"]["rows"][0]["LABEL0"] == -1.0


def test_dataset_preview_seals_test_and_bounds_rows() -> None:
    with pytest.raises(ModelResearchConflict, match="只开放"):
        _preview_request({"split": "test"})
    with pytest.raises(ModelResearchConflict, match="1 至 500"):
        _preview_request({"rows": 501})
