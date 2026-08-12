from __future__ import annotations

from datetime import date

from factor_service.api import metadata


def test_source_range_uses_short_lived_cache(monkeypatch):
    class Result:
        result_rows = [(date(2020, 1, 2), date(2026, 8, 10))]

    class FakeClient:
        calls = 0

        def query(self, sql):
            self.calls += 1
            assert "SELECT min(trade_time)" in sql
            assert "count()" not in sql
            return Result()

    fake = FakeClient()
    monkeypatch.setattr(metadata, "client", lambda: fake)
    monkeypatch.setattr(metadata, "_source_range_cache", {})

    first = metadata.source_range(entity_type="stock")
    second = metadata.source_range(entity_type="stock")

    assert first["date_start"] == date(2020, 1, 2)
    assert first["date_end"] == date(2026, 8, 10)
    assert second == first
    assert fake.calls == 1
