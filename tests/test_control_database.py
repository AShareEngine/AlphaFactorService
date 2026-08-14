from concurrent.futures import ThreadPoolExecutor
import time

from factor_service import control_database


class _FakePool:
    instances: list["_FakePool"] = []

    def __init__(self, **_kwargs) -> None:
        self.closed = True
        self.open_calls = 0
        self.close_calls = 0
        self.__class__.instances.append(self)

    def open(self, *, wait: bool, timeout: float) -> None:
        assert wait is True
        assert timeout == 5.0
        time.sleep(0.01)
        if not self.closed:
            raise RuntimeError("pool opened twice")
        self.open_calls += 1
        self.closed = False

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class _FailingPool(_FakePool):
    def open(self, *, wait: bool, timeout: float) -> None:
        self.open_calls += 1
        raise TimeoutError("database unavailable")


def _config() -> control_database.ControlDatabaseConfig:
    return control_database.ControlDatabaseConfig(
        host="127.0.0.1",
        port=5432,
        database="alphablocks",
        schema="control",
        username="postgres",
        password="",
        sslmode="prefer",
        connect_timeout_seconds=5,
        min_pool_size=1,
        max_pool_size=2,
    )


def test_control_database_opens_pool_once_for_concurrent_first_use(monkeypatch) -> None:
    _FakePool.instances.clear()
    monkeypatch.setattr(control_database, "ConnectionPool", _FakePool)
    database = control_database.ControlDatabase(_config())

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: database.open(), range(16)))

    assert len(_FakePool.instances) == 1
    assert _FakePool.instances[0].open_calls == 1


def test_control_database_rebuilds_pool_after_close(monkeypatch) -> None:
    _FakePool.instances.clear()
    monkeypatch.setattr(control_database, "ConnectionPool", _FakePool)
    database = control_database.ControlDatabase(_config())

    database.open()
    first_pool = _FakePool.instances[0]
    database.close()
    database.open()

    assert first_pool.close_calls == 1
    assert len(_FakePool.instances) == 2
    assert _FakePool.instances[1].open_calls == 1


def test_control_database_rebuilds_pool_after_open_failure(monkeypatch) -> None:
    _FakePool.instances.clear()
    created = 0

    def pool_factory(**kwargs):
        nonlocal created
        created += 1
        pool_type = _FailingPool if created == 1 else _FakePool
        return pool_type(**kwargs)

    monkeypatch.setattr(control_database, "ConnectionPool", pool_factory)
    database = control_database.ControlDatabase(_config())

    try:
        database.open()
    except TimeoutError:
        pass
    else:
        raise AssertionError("first pool initialization must fail")
    database.open()

    assert created == 2
    assert _FakePool.instances[0].close_calls == 1
    assert _FakePool.instances[1].open_calls == 1
