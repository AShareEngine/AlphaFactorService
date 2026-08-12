from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi import HTTPException

from factor_service import repository
from factor_service.api.values import _validate_visibility_cutoff


def test_coverage_returns_actual_persisted_date_range(monkeypatch):
    class Result:
        result_rows = [(42, 5, 3, date(2020, 1, 2), date(2020, 1, 6))]

    class FakeClient:
        def query(self, sql, parameters):
            assert "min(trade_date) AS date_start" in sql
            assert parameters["date_start"] == date(2019, 1, 1)
            return Result()

    monkeypatch.setattr(repository, "client", lambda: FakeClient())

    actual = repository.coverage(
        factor_id="demo",
        date_start=date(2019, 1, 1),
        date_end=date(2026, 1, 1),
    )

    assert actual.date_start == date(2020, 1, 2)
    assert actual.date_end == date(2020, 1, 6)
    assert actual.rows == 42


def test_coverage_without_rows_has_no_date_range(monkeypatch):
    class Result:
        result_rows = [(0, 0, 0, None, None)]

    class FakeClient:
        def query(self, sql, parameters):
            return Result()

    monkeypatch.setattr(repository, "client", lambda: FakeClient())

    actual = repository.coverage(factor_id="demo")

    assert actual.date_start is None
    assert actual.date_end is None


def test_factor_value_api_rejects_implicit_latest_visibility():
    with pytest.raises(HTTPException, match="读取因子值必须提供"):
        _validate_visibility_cutoff(
            available_before=None,
            event_available_before=None,
            allow_latest=False,
        )


def test_factor_value_api_rejects_mixed_visibility_cutoffs():
    cutoff = datetime(2024, 1, 3, 15, 0)

    with pytest.raises(HTTPException, match="不能同时使用"):
        _validate_visibility_cutoff(
            available_before=cutoff,
            event_available_before=cutoff,
            allow_latest=False,
        )
