from __future__ import annotations

from collections import Counter
from pathlib import Path

from factor_service.qlib_formula import compile_qlib_formula
from scripts.audit_joinquant_factor_compatibility import (
    ENGINE,
    READY,
    REVIEW,
    build_translations,
    parse_catalog,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FIELDS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "pre_close",
    "turnover_rate",
    "pct_chg",
    "pe",
    "pb",
}


def test_joinquant_catalog_and_audit_registry_stay_in_sync():
    factors = parse_catalog(REPOSITORY_ROOT / "docs/joinquant-factor-catalog.md")
    translations = build_translations()

    assert len(factors) == 285
    assert set(translations) <= {factor.name for factor in factors}
    assert Counter(item.status for item in translations.values()) == {
        READY: 84,
        REVIEW: 9,
        ENGINE: 15,
    }


def test_every_ready_joinquant_expression_compiles_against_current_source_fields():
    for name, translation in build_translations().items():
        if translation.status != READY:
            continue

        compiled = compile_qlib_formula(
            translation.expression or "",
            params={},
            code_column="code",
            date_column="trade_time",
        )

        assert set(compiled.fields) <= SOURCE_FIELDS, name
