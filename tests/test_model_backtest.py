from __future__ import annotations

import pandas as pd

from factor_service.model_backtest import _build_top_n_targets


def _market(calendar: pd.DatetimeIndex, codes: list[str]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "date": trade_date,
            "code": code,
            "forward_return": 0.01,
            "buy_allowed": True,
            "sell_allowed": True,
            "is_st": 0,
            "is_withdrawal": 0,
        }
        for trade_date in calendar
        for code in codes
    ])


def test_top_n_uses_next_session_and_stable_score_order() -> None:
    calendar = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04"])
    codes = ["000001.SZ", "000002.SZ", "000003.SZ"]
    signals = pd.DataFrame({
        "signal_date": [pd.Timestamp("2024-01-02")] * 3,
        "code": codes,
        "score": [0.9, 0.4, 0.9],
    })

    targets, counts = _build_top_n_targets(
        signals, _market(calendar, codes), calendar,
        top_n=2, rebalance_every=1,
        configuration={"exclude_limit_paused": False},
    )

    execution_date = pd.Timestamp("2024-01-03")
    assert targets[execution_date] == {"000001.SZ": 0.5, "000003.SZ": 0.5}
    assert counts[execution_date] == 3


def test_top_n_rebalances_every_five_signal_sessions() -> None:
    calendar = pd.date_range("2024-01-02", periods=8, freq="B")
    codes = ["000001.SZ", "000002.SZ"]
    signals = pd.DataFrame([
        {"signal_date": day, "code": code, "score": float(index)}
        for day in calendar[:6]
        for index, code in enumerate(codes)
    ])

    targets, _ = _build_top_n_targets(
        signals, _market(calendar, codes), calendar,
        top_n=1, rebalance_every=5,
        configuration={"exclude_limit_paused": False},
    )

    assert list(targets) == [calendar[1], calendar[6]]
