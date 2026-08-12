from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local

from factor_service import clickhouse


def test_client_is_reused_per_thread_but_not_shared_between_threads(monkeypatch):
    created = []
    barrier = Barrier(2)

    def fake_get_client(**kwargs):
        value = object()
        created.append((value, kwargs))
        return value

    monkeypatch.setattr(clickhouse, "_client_state", local())
    monkeypatch.setattr(clickhouse.clickhouse_connect, "get_client", fake_get_client)

    def get_twice():
        first = clickhouse.client()
        barrier.wait()
        return first, clickhouse.client()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: get_twice(), range(2)))

    assert len(created) == 2
    assert results[0][0] is results[0][1]
    assert results[1][0] is results[1][1]
    assert results[0][0] is not results[1][0]
    assert all(kwargs["autogenerate_session_id"] is False for _, kwargs in created)


def test_legacy_available_at_migration_is_metadata_only():
    commands = []

    class Result:
        def __init__(self, rows):
            self.result_rows = rows

    class FakeClient:
        def query(self, sql):
            if sql.startswith("DESCRIBE"):
                return Result([("available_at",), ("computed_at",)])
            assert "countIf(available_at != computed_at)" in sql
            return Result([(12,)])

        def command(self, sql):
            commands.append(sql)

    clickhouse._migrate_legacy_available_at(FakeClient(), "ab_factor")

    assert len(commands) == 2
    assert "RENAME COLUMN available_at TO legacy_available_at" in commands[0]
    assert "ADD COLUMN available_at" in commands[1]
    assert not any("UPDATE" in sql for sql in commands)


def test_legacy_available_at_migration_is_idempotent():
    class Result:
        result_rows = [("available_at",), ("legacy_available_at",)]

    class FakeClient:
        def query(self, sql):
            return Result()

        def command(self, sql):
            raise AssertionError("already migrated schema must not be altered")

    clickhouse._migrate_legacy_available_at(FakeClient(), "ab_factor")
