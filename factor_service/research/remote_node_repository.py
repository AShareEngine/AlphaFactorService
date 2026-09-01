from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Mapping

from psycopg.types.json import Jsonb

from factor_service.control_database import ControlDatabase, get_control_database
from factor_service.research.remote_node_secrets import RemoteNodeSecretCipher


_SECRET_FIELDS = {"api_token", "ssh_password", "ssh_private_key"}


class RemoteNodeRepository:
    """PostgreSQL source of truth for remote model execution nodes."""

    def __init__(
        self,
        database: ControlDatabase | None = None,
        secret_cipher: RemoteNodeSecretCipher | None = None,
    ) -> None:
        self.database = database or get_control_database()
        self._secret_cipher = secret_cipher

    def list_nodes(self) -> list[dict[str, Any]]:
        with self.database.connection() as conn:
            rows = conn.execute(
                """
                SELECT nodes.node_id, nodes.config_json,
                       secrets.encrypted_payload, secrets.encryption_version
                FROM model_execution_nodes AS nodes
                LEFT JOIN model_execution_node_secrets AS secrets
                    ON secrets.node_id = nodes.node_id
                ORDER BY nodes.sort_order, nodes.created_at, nodes.node_id
                """
            ).fetchall()
        return [self._stored_payload(row) for row in rows]

    def create_node(
        self,
        payload: Mapping[str, Any],
        secrets: Mapping[str, Any],
    ) -> dict[str, Any]:
        clean = _payload(payload)
        encrypted = self._encrypt(clean["id"], secrets)
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
                if row and encrypted is not None:
                    conn.execute(
                        """
                        INSERT INTO model_execution_node_secrets(
                            node_id, encrypted_payload, encryption_version,
                            created_at, updated_at
                        ) VALUES (%s, %s, 1, %s, %s)
                        """,
                        (clean["id"], encrypted, now, now),
                    )
        if not row:
            raise ValueError(f"远程训练节点已存在: {clean['id']}")
        return {**clean, **_string_secrets(secrets)}

    def update_node(
        self,
        node_id: str,
        payload: Mapping[str, Any],
        secrets: Mapping[str, Any],
    ) -> dict[str, Any]:
        clean_id = str(node_id or "").strip()
        clean = _payload(payload)
        if clean["id"] != clean_id:
            raise ValueError("远程训练节点ID创建后不可修改")
        encrypted = self._encrypt(clean_id, secrets)
        now = _utcnow()
        with self.database.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    UPDATE model_execution_nodes
                    SET config_json = %s, updated_at = %s
                    WHERE node_id = %s
                    RETURNING node_id, config_json
                    """,
                    (Jsonb(clean), now, clean_id),
                ).fetchone()
                if row and encrypted is not None:
                    conn.execute(
                        """
                        INSERT INTO model_execution_node_secrets(
                            node_id, encrypted_payload, encryption_version,
                            created_at, updated_at
                        ) VALUES (%s, %s, 1, %s, %s)
                        ON CONFLICT (node_id) DO UPDATE
                        SET encrypted_payload = EXCLUDED.encrypted_payload,
                            encryption_version = EXCLUDED.encryption_version,
                            updated_at = EXCLUDED.updated_at
                        """,
                        (clean_id, encrypted, now, now),
                    )
                elif row:
                    conn.execute(
                        "DELETE FROM model_execution_node_secrets WHERE node_id = %s",
                        (clean_id,),
                    )
        if not row:
            raise ValueError(f"远程训练节点未配置: {clean_id}")
        return {**clean, **_string_secrets(secrets)}

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

    def _encrypt(
        self, node_id: str, secrets: Mapping[str, Any],
    ) -> bytes | None:
        clean = _string_secrets(secrets)
        if not clean:
            return None
        return self._cipher().encrypt(node_id, clean)

    def _stored_payload(self, row: Mapping[str, Any]) -> dict[str, Any]:
        payload = _stored_config(row)
        encrypted = row.get("encrypted_payload")
        if encrypted is not None:
            if int(row.get("encryption_version") or 0) != 1:
                raise ValueError("PostgreSQL远程节点秘密版本不受支持")
            payload.update(self._cipher().decrypt(payload["id"], encrypted))
        return payload

    def _cipher(self) -> RemoteNodeSecretCipher:
        if self._secret_cipher is None:
            self._secret_cipher = RemoteNodeSecretCipher.from_environment()
        return self._secret_cipher


@lru_cache(maxsize=1)
def get_remote_node_repository() -> RemoteNodeRepository:
    return RemoteNodeRepository()


def _payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    clean = dict(payload or {})
    forbidden = _SECRET_FIELDS & set(clean)
    if forbidden:
        raise ValueError("远程节点秘密不能写入config_json")
    node_id = str(clean.get("id") or "").strip()
    if not node_id:
        raise ValueError("远程训练节点ID不能为空")
    clean["id"] = node_id
    return clean


def _stored_config(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("config_json") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("PostgreSQL远程训练节点配置必须是对象")
    payload = dict(raw)
    payload["id"] = str(row.get("node_id") or payload.get("id") or "").strip()
    return payload


def _string_secrets(payload: Mapping[str, Any]) -> dict[str, str]:
    unknown = set(payload) - _SECRET_FIELDS
    if unknown:
        raise ValueError("远程节点秘密包含不支持字段: " + ", ".join(sorted(unknown)))
    return {
        field: str(payload.get(field) or "")
        for field in sorted(_SECRET_FIELDS)
        if str(payload.get(field) or "")
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["RemoteNodeRepository", "get_remote_node_repository"]
