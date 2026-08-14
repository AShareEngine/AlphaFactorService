from __future__ import annotations

from contextlib import contextmanager
import atexit
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
from typing import Any, Iterator

from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from factor_service.runtime_config import (
    DEFAULT_RUNTIME_CONFIG_PATH,
    load_runtime_config,
    section,
)


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OPEN_DATABASES: list["ControlDatabase"] = []


@dataclass(frozen=True)
class ControlDatabaseConfig:
    host: str
    port: int
    database: str
    schema: str
    username: str
    password: str
    sslmode: str
    connect_timeout_seconds: int
    min_pool_size: int
    max_pool_size: int


class ControlDatabaseConfigurationError(ValueError):
    pass


class ControlDatabaseSchemaError(RuntimeError):
    pass


class ControlDatabase:
    """Bounded psycopg pool for the PostgreSQL registry control plane."""

    def __init__(self, config: ControlDatabaseConfig) -> None:
        self.config = _validated_config(config)
        self._pool = ConnectionPool(
            kwargs=_connection_kwargs(self.config),
            min_size=max(1, int(self.config.min_pool_size)),
            max_size=max(
                max(1, int(self.config.min_pool_size)),
                int(self.config.max_pool_size),
            ),
            open=False,
            configure=self._configure_connection,
            name="alphablocks-control",
        )
        _OPEN_DATABASES.append(self)

    @property
    def schema(self) -> str:
        return self.config.schema

    def open(self, *, wait: bool = True) -> None:
        self._pool.open(wait=wait, timeout=float(self.config.connect_timeout_seconds))

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self) -> Iterator[Connection[dict[str, Any]]]:
        if self._pool.closed:
            self.open()
        with self._pool.connection() as conn:
            yield conn

    def dedicated_connection(
        self,
        *,
        autocommit: bool = False,
    ) -> Connection[dict[str, Any]]:
        """Open a caller-owned connection for long-lived database features."""

        conn = Connection.connect(**_connection_kwargs(self.config))
        self._configure_connection(conn)
        conn.autocommit = bool(autocommit)
        return conn

    def check(self) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT current_database() AS database, current_schema() AS schema, version() AS version"
            ).fetchone()
        return dict(row or {})

    def _configure_connection(self, conn: Connection[Any]) -> None:
        conn.row_factory = dict_row
        conn.execute(
            sql.SQL("SET search_path TO {}, public").format(
                sql.Identifier(self.config.schema)
            )
        )
        conn.commit()


def build_control_database(
    runtime_path: str | Path = DEFAULT_RUNTIME_CONFIG_PATH,
) -> ControlDatabase:
    runtime = load_runtime_config(runtime_path)
    payload = section(runtime, "control_database")
    return ControlDatabase(ControlDatabaseConfig(
        host=str(payload.get("host") or "127.0.0.1").strip(),
        port=int(payload.get("port") or 5432),
        database=str(payload.get("database") or "alphablocks").strip(),
        schema=str(payload.get("schema") or "control").strip(),
        username=str(payload.get("username") or "postgres").strip(),
        password=str(payload.get("password") or ""),
        sslmode=str(payload.get("sslmode") or "prefer").strip(),
        connect_timeout_seconds=int(payload.get("connect_timeout_seconds") or 5),
        min_pool_size=int(payload.get("min_pool_size") or 1),
        max_pool_size=int(payload.get("max_pool_size") or 5),
    ))


@lru_cache(maxsize=8)
def get_control_database(
    runtime_path: str | Path = DEFAULT_RUNTIME_CONFIG_PATH,
) -> ControlDatabase:
    return build_control_database(Path(runtime_path).resolve())


def _validated_config(config: ControlDatabaseConfig) -> ControlDatabaseConfig:
    missing = [
        name
        for name in ("host", "database", "schema", "username")
        if not str(getattr(config, name, "") or "").strip()
    ]
    if missing:
        raise ControlDatabaseConfigurationError(
            "control_database is missing required settings: " + ", ".join(missing)
        )
    if not _IDENTIFIER.fullmatch(str(config.schema)):
        raise ControlDatabaseConfigurationError("control_database.schema is not a valid identifier")
    if not (1 <= int(config.port) <= 65535):
        raise ControlDatabaseConfigurationError("control_database.port is invalid")
    if int(config.connect_timeout_seconds) <= 0:
        raise ControlDatabaseConfigurationError(
            "control_database.connect_timeout_seconds must be positive"
        )
    if int(config.min_pool_size) <= 0 or int(config.max_pool_size) < int(config.min_pool_size):
        raise ControlDatabaseConfigurationError(
            "control_database pool sizes are invalid"
        )
    return config


def _connection_kwargs(
    config: ControlDatabaseConfig,
    *,
    database: str | None = None,
) -> dict[str, Any]:
    return {
        "host": config.host,
        "port": int(config.port),
        "dbname": database or config.database,
        "user": config.username,
        "password": config.password,
        "sslmode": config.sslmode or "prefer",
        "connect_timeout": int(config.connect_timeout_seconds),
        "application_name": "alpha-factor-service-model-research",
        "row_factory": dict_row,
    }


def _close_open_databases() -> None:
    for database in reversed(_OPEN_DATABASES):
        try:
            database.close()
        except Exception:
            pass


atexit.register(_close_open_databases)


__all__ = [
    "ControlDatabase",
    "ControlDatabaseConfig",
    "ControlDatabaseConfigurationError",
    "ControlDatabaseSchemaError",
    "build_control_database",
    "get_control_database",
]
