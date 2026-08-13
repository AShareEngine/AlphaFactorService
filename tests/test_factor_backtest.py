from __future__ import annotations

import pandas as pd
import pytest

from factor_service.factor_backtest import (
    _annualized_return,
    _build_targets,
    _max_drawdown,
    _normalize_signal_dates,
    _simulate_quantile_portfolio,
)


def _market(calendar: pd.DatetimeIndex, codes: list[str]) -> pd.DataFrame:
    rows = []
    for trade_date in calendar:
        for index, code in enumerate(codes):
            rows.append({
                "date": trade_date,
                "code": code,
                "forward_return": index / 1000,
                "buy_allowed": True,
                "sell_allowed": True,
                "is_st": 0,
                "is_withdrawal": 0,
            })
    return pd.DataFrame(rows)


def test_close_signal_is_executed_on_next_trading_day():
    calendar = pd.DatetimeIndex(["2024-01-05", "2024-01-08", "2024-01-09"])
    codes = [f"{index:06d}.SZ" for index in range(25)]
    signals = pd.DataFrame({
        "signal_date": pd.Timestamp("2024-01-05"),
        "code": codes,
        "score": list(range(25)),
    })

    q1, q5, ic, counts = _build_targets(signals, _market(calendar, codes), calendar)

    execution_date = pd.Timestamp("2024-01-08")
    assert list(q1) == [execution_date]
    assert list(q5) == [execution_date]
    assert set(q1[execution_date]) == set(codes[:5])
    assert set(q5[execution_date]) == set(codes[-5:])
    assert ic[execution_date] == pytest.approx(1.0)
    assert counts[execution_date] == 25


def test_event_availability_delays_the_signal_date_without_lookahead():
    frame = pd.DataFrame([
        {
            "source_trade_date": "2024-01-05",
            "code": "000001.SZ",
            "score": 1.2,
            "event_available_at": "2024-01-08 18:00:00+08:00",
        },
    ])

    normalized = _normalize_signal_dates(frame)

    assert normalized.iloc[0]["signal_date"] == pd.Timestamp("2024-01-08")


def test_blocked_buy_stays_cash_and_is_reported():
    calendar = pd.DatetimeIndex(["2024-01-08", "2024-01-09"])
    market = pd.DataFrame([
        {
            "date": pd.Timestamp("2024-01-08"),
            "code": "000001.SZ",
            "forward_return": 0.10,
            "buy_allowed": False,
            "sell_allowed": True,
            "is_st": 0,
            "is_withdrawal": 0,
        },
    ])

    result = _simulate_quantile_portfolio(
        {pd.Timestamp("2024-01-08"): {"000001.SZ": 1.0}},
        market,
        calendar,
        buy_cost_rate=0.0003,
        sell_cost_rate=0.0013,
    )[pd.Timestamp("2024-01-08")]

    assert result.blocked_buy_count == 1
    assert result.net_return == pytest.approx(0.0)
    assert result.cost == pytest.approx(0.0)


def test_actual_buy_weight_is_charged_buy_cost():
    calendar = pd.DatetimeIndex(["2024-01-08", "2024-01-09"])
    market = pd.DataFrame([
        {
            "date": pd.Timestamp("2024-01-08"),
            "code": "000001.SZ",
            "forward_return": 0.01,
            "buy_allowed": True,
            "sell_allowed": True,
            "is_st": 0,
            "is_withdrawal": 0,
        },
    ])

    result = _simulate_quantile_portfolio(
        {pd.Timestamp("2024-01-08"): {"000001.SZ": 1.0}},
        market,
        calendar,
        buy_cost_rate=0.0003,
        sell_cost_rate=0.0013,
    )[pd.Timestamp("2024-01-08")]

    assert result.cost == pytest.approx(0.0003)
    assert result.net_return == pytest.approx(0.0097)


def test_missing_signal_day_keeps_existing_portfolio_instead_of_liquidating():
    calendar = pd.DatetimeIndex(["2024-01-08", "2024-01-09", "2024-01-10"])
    market = pd.DataFrame([
        {
            "date": trade_date,
            "code": "000001.SZ",
            "forward_return": 0.01,
            "buy_allowed": True,
            "sell_allowed": True,
            "is_st": 0,
            "is_withdrawal": 0,
        }
        for trade_date in calendar[:-1]
    ])

    result = _simulate_quantile_portfolio(
        {pd.Timestamp("2024-01-08"): {"000001.SZ": 1.0}},
        market,
        calendar,
        buy_cost_rate=0.0003,
        sell_cost_rate=0.0013,
    )

    assert result[pd.Timestamp("2024-01-08")].cost == pytest.approx(0.0003)
    assert result[pd.Timestamp("2024-01-09")].cost == pytest.approx(0.0)
    assert result[pd.Timestamp("2024-01-09")].net_return == pytest.approx(0.01)


def test_summary_math_uses_trading_day_annualization_and_negative_drawdown():
    daily = [0.001] * 252
    assert _annualized_return(daily) == pytest.approx((1.001 ** 252) - 1)
    assert _max_drawdown(pd.Series([1.0, 1.1, 0.88, 1.2])) == pytest.approx(-0.2)
