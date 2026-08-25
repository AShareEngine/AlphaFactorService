from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import threading
from typing import Any, Mapping, Sequence

from psycopg.types.json import Jsonb

from factor_service.control_database import ControlDatabase, get_control_database


_LEGACY_MIGRATION_KEY = "runtime_yaml_remote_nodes_v1"


class RemoteNodeRepository:
    """PostgreSQL source of truth for remote model execution nodes."""

    def __init__(self, database: ControlDatabase | None = None) -> None:
        self.database = database or get_control_database()
        self._legacy_import_checked = False
        self._legacy_import_lock = threading.Lock()

    def list_nodes(self) -> list[dict[str, Any]]:
        with self.database.connection() as conn:
            rows = conn.execute(
                """
                SELECT node_id, config_json
                FROM model_execution_nodes
                ORDER BY sort_order, created_at, node_id
                """
            ).fetchall()
        return [_stored_payload(row) for row in rows]

    def create_node(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        clean = _payload(payload)
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO model_execution_nodes(
                        node_id, config_json, sort_order, created_at, updated_at
                    )
                    SELECT %s, %s, COALESCE(max(sort_order), -1) + 1, %s, %s
                    FROM model_execution_nodes
                    ON CONFLICT (node_id) DO NOTHING
                    RETURNING node_id, config_json
                    """,
                    (
                        clean["id"], Jsonb(clean), now, now,
                    ),
                ).fetchone()
        if not row:
            raise ValueError(f"远程训练节点已存在: {clean['id']}")
        return _stored_payload(row)

    def update_node(
        self, node_id: str, payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        clean_id = str(node_id or "").strip()
        clean = _payload(payload)
        if clean["id"] != clean_id:
            raise ValueError("远程训练节点ID创建后不可修改")
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    UPDATE model_execution_nodes
                    SET config_json = %s, updated_at = %s
                    WHERE node_id = %s
                    RETURNING node_id, config_json
                    """,
                    (Jsonb(clean), _utcnow(), clean_id),
                ).fetchone()
        if not row:
            raise ValueError(f"远程训练节点未配置: {clean_id}")
        return _stored_payload(row)

    def delete_node(self, node_id: str) -> bool:
        clean_id = str(node_id or "").strip()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    DELETE FROM model_execution_nodes
                    WHERE node_id = %s
                    RETURNING node_id
                    """,
                    (clean_id,),
                ).fetchone()
        return bool(row)

    def legacy_import_required(self) -> bool:
        if self._legacy_import_checked:
            return False
        with self.database.connection() as conn:
            migrated = conn.execute(
                """
                SELECT migration_key
                FROM model_execution_node_migrations
                WHERE migration_key = %s
                """,
                (_LEGACY_MIGRATION_KEY,),
            ).fetchone()
        if migrated:
            self._legacy_import_checked = True
            return False
        return True

    def import_legacy_once(
        self, payloads: Sequence[Mapping[str, Any]],
    ) -> bool:
        """Import the old YAML node list exactly once across all service replicas."""

        if self._legacy_import_checked:
            return False
        normalized = [_payload(payload) for payload in payloads]
        now = _utcnow()
        with self._legacy_import_lock:
            if self._legacy_import_checked:
                return False
            with self.database.connection() as conn:
                with conn.transaction():
                    conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        ("model_execution_nodes:legacy_import",),
                    )
                    migrated = conn.execute(
                        """
                        SELECT migration_key
                        FROM model_execution_node_migrations
                        WHERE migration_key = %s
                        """,
                        (_LEGACY_MIGRATION_KEY,),
                    ).fetchone()
                    if not migrated:
                        for sort_order, payload in enumerate(normalized):
                            conn.execute(
                                """
                                INSERT INTO model_execution_nodes(
                                    node_id, config_json, sort_order,
                                    created_at, updated_at
                                ) VALUES (%s, %s, %s, %s, %s)
                                ON CONFLICT (node_id) DO NOTHING
                                """,
                                (
                                    payload["id"], Jsonb(payload), sort_order,
                                    now, now,
                                ),
                            )
                        conn.execute(
                            """
                            INSERT INTO model_execution_node_migrations(
                                migration_key, imported_count, completed_at
                            ) VALUES (%s, %s, %s)
                            """,
                            (_LEGACY_MIGRATION_KEY, len(normalized), now),
                        )
            self._legacy_import_checked = True
            return not bool(migrated)


@lru_cache(maxsize=1)
def get_remote_node_repository() -> RemoteNodeRepository:
    return RemoteNodeRepository()


def _payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    clean = dict(payload or {})
    node_id = str(clean.get("id") or "").strip()
    if not node_id:
        raise ValueError("远程训练节点ID不能为空")
    clean["id"] = node_id
    return clean


def _stored_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("config_json") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("PostgreSQL远程训练节点配置必须是对象")
    payload = dict(raw)
    payload["id"] = str(row.get("node_id") or payload.get("id") or "").strip()
    return payload


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["RemoteNodeRepository", "get_remote_node_repository"]
