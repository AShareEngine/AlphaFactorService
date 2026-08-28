from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pandas as pd
from fastapi import BackgroundTasks

from factor_service import analysis
from factor_service.analysis import AnalysisPayload, _shift_factor_to_next_trading_day
from factor_service.api import analysis as analysis_api
from factor_service.schemas import FactorFormulaAnalysisRequest


def test_close_factor_is_shifted_to_next_trading_day():
    index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2024-01-05"), "000001.SZ"),
            (pd.Timestamp("2024-01-08"), "000001.SZ"),
        ],
        names=["date", "asset"],
    )
    factor = pd.Series([1.0, 2.0], index=index, name="factor")
    calendar = pd.DatetimeIndex(
        ["2024-01-05", "2024-01-08", "2024-01-09"]
    )

    shifted = _shift_factor_to_next_trading_day(factor, calendar)

    assert shifted.index.get_level_values("date").tolist() == [
        pd.Timestamp("2024-01-08"),
        pd.Timestamp("2024-01-09"),
    ]
    assert shifted.tolist() == [1.0, 2.0]


def test_start_analysis_schedules_pending_job_without_blocking(monkeypatch):
    class Job:
        status = "pending"

    monkeypatch.setattr(
        analysis_api.repository,
        "get_analysis_job",
        lambda analysis_job_id: Job() if analysis_job_id == "analysis-1" else None,
    )
    background = BackgroundTasks()

    returned = analysis_api.start_one_analysis_job("analysis-1", background)

    assert returned.status == "pending"
    assert len(background.tasks) == 1
    assert background.tasks[0].func is analysis_api.run_analysis_job
    assert background.tasks[0].args == ("analysis-1",)


def test_formula_analysis_is_ephemeral_and_returns_metric_tables(monkeypatch):
    @contextmanager
    def source(*args, **kwargs):
        yield SimpleNamespace()

    class QueryResult:
        result_rows = [
            (pd.Timestamp("2024-01-02"), "000001.SZ", 0.1, 1, 0.5, 0.2),
        ]

    class Client:
        def query(self, sql, parameters=None):
            return QueryResult()

    monkeypatch.setattr(analysis, "factor_query_source", source)
    monkeypatch.setattr(
        analysis,
        "build_factor_query_plan",
        lambda *args, **kwargs: SimpleNamespace(
            sql="SELECT 1", params={}, params_hash="params-hash",
        ),
    )
    monkeypatch.setattr(analysis, "client", lambda: Client())
    monkeypatch.setattr(
        analysis,
        "_analyze_factor_series",
        lambda *args, **kwargs: AnalysisPayload(
            summary_rows=[("formula-1", "ic_mean", "5D", 0.05, "{}")],
            ic_rows=[("formula-1", pd.Timestamp("2024-01-03").date(), "5D", 0.05)],
            quantile_return_rows=[
                ("formula-1", pd.Timestamp("2024-01-03").date(), "5D", 5, 0.01)
            ],
            turnover_rows=[
                ("formula-1", pd.Timestamp("2024-01-03").date(), "5D", 5, 0.2, 0.8)
            ],
            row_count=1,
        ),
    )

    result = analysis.run_formula_analysis(FactorFormulaAnalysisRequest(
        name="mom_20",
        expression="$close / Ref($close, 20) - 1",
        date_start="2024-01-02",
        date_end="2024-03-29",
        periods=[5],
    ))

    assert result["registered"] is False
    assert result["persisted"] is False
    assert result["summary"][0]["metric"] == "ic_mean"
    assert result["ic"][0]["ic"] == 0.05
    assert result["quantile_returns"][0]["quantile"] == 5
    assert result["turnover"][0]["turnover"] == 0.2
