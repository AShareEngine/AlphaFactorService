from __future__ import annotations

import pandas as pd

from factor_service.analysis import _shift_factor_to_next_trading_day


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
