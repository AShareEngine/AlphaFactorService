from __future__ import annotations

from collections import Counter
from pathlib import Path

from factor_service.qlib_formula import compile_qlib_formula
from scripts.import_joinquant_ready_factors import build_factor_payloads


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_builds_all_audited_joinquant_factor_definitions():
    payloads = build_factor_payloads(REPOSITORY_ROOT / "docs/joinquant-factor-catalog.md")

    assert len(payloads) == 84
    assert len({payload.factor_id for payload in payloads}) == 84
    assert Counter(payload.category for payload in payloads) == {
        "风险因子 - 风格因子": 5,
        "情绪类因子": 28,
        "风险类因子": 12,
        "技术指标因子": 13,
        "动量类因子": 25,
        "风险因子 - 新风格因子": 1,
    }


def test_imported_joinquant_definitions_are_compute_ready():
    payloads = build_factor_payloads(REPOSITORY_ROOT / "docs/joinquant-factor-catalog.md")

    for payload in payloads:
        compiled = compile_qlib_formula(
            payload.expression,
            params=payload.params,
            code_column="code",
            date_column="trade_time",
        )
        assert compiled.fields, payload.factor_id
        assert payload.params["_specs"][0]["is_default"] is True
        assert payload.params["_specs"][0]["enabled"] is True
        assert payload.params["data_processing"]["neutralize"] == []


def test_joinquant_no_processing_factors_preserve_raw_score():
    payloads = {
        payload.factor_id: payload
        for payload in build_factor_payloads(REPOSITORY_ROOT / "docs/joinquant-factor-catalog.md")
    }

    for factor_id in {
        "average_share_turnover_annual",
        "average_share_turnover_quarterly",
        "book_to_price_ratio",
        "earnings_to_price_ratio",
        "share_turnover_monthly",
        "btop",
    }:
        processing = payloads[factor_id].params["data_processing"]
        assert processing["winsorize"] == "none"
        assert processing["standardize"] == "none"
