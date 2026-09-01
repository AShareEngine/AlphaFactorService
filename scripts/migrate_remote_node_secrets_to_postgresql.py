from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Mapping

from factor_service.control_database import get_control_database
from factor_service.research.remote_node_repository import RemoteNodeRepository
from factor_service.research.remote_node_secrets import RemoteNodeSecretCipher


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKEN_FILE = PROJECT_ROOT / ".secrets" / "autodl_api_token"
_LEGACY_FIELDS = {"api_token_env", "password_env", "ssh_key"}


def migrate(*, apply: bool) -> dict[str, Any]:
    database = get_control_database()
    cipher = RemoteNodeSecretCipher.from_environment()
    with database.connection() as conn:
        rows = conn.execute(
            """
            SELECT nodes.node_id, nodes.config_json,
                   (secrets.node_id IS NOT NULL) AS already_migrated
            FROM model_execution_nodes AS nodes
            LEFT JOIN model_execution_node_secrets AS secrets
                ON secrets.node_id = nodes.node_id
            ORDER BY nodes.sort_order, nodes.created_at, nodes.node_id
            """,
        ).fetchall()
    repository = RemoteNodeRepository(database, cipher)
    migrated: list[str] = []
    skipped: list[str] = []
    for row in rows:
        node_id = str(row["node_id"])
        if bool(row.get("already_migrated")):
            skipped.append(node_id)
            continue
        config = dict(row.get("config_json") or {})
        clean, secrets = legacy_node_payload(node_id, config)
        if apply:
            repository.update_node(node_id, clean, secrets)
        migrated.append(node_id)
    return {
        "apply": apply,
        "migrated": migrated,
        "skipped": skipped,
        "count": len(migrated),
    }


def legacy_node_payload(
    node_id: str,
    source: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    clean = dict(source)
    clean["id"] = node_id
    authentication_type = str(clean.get("authentication_type") or "").strip()
    key_reference = str(clean.get("ssh_key") or "").strip()
    password_environment = str(clean.get("password_env") or "").strip()
    if not authentication_type:
        authentication_type = "ssh_private_key" if key_reference else "password"
    secrets = {
        "ssh_private_key": "",
        "ssh_password": "",
        "api_token": "",
    }
    if authentication_type == "ssh_private_key":
        if not key_reference:
            raise ValueError(f"节点{node_id}缺少旧SSH私钥路径")
        key_path = Path(key_reference).expanduser()
        if not key_path.is_absolute():
            key_path = PROJECT_ROOT / key_path
        if not key_path.is_file():
            raise ValueError(f"节点{node_id}的旧SSH私钥不存在: {key_path}")
        secrets["ssh_private_key"] = key_path.read_text(encoding="utf-8")
    elif authentication_type == "password":
        password = str(os.environ.get(password_environment) or "")
        if not password:
            raise ValueError(f"节点{node_id}的旧SSH密码环境变量不可用")
        secrets["ssh_password"] = password
    else:
        raise ValueError(f"节点{node_id}的认证类型无效: {authentication_type}")
    if str(clean.get("lifecycle_provider") or "").strip() == "autodl_pro":
        token_environment = str(clean.get("api_token_env") or "").strip()
        token = str(os.environ.get(token_environment) or "").strip()
        token_path = Path(
            str(os.environ.get("ALPHA_AUTODL_API_TOKEN_FILE") or DEFAULT_TOKEN_FILE),
        ).expanduser()
        if not token and token_path.is_file():
            token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError(f"节点{node_id}的旧AutoDL Token不可用")
        secrets["api_token"] = token
    for field in _LEGACY_FIELDS:
        clean.pop(field, None)
    clean["authentication_type"] = authentication_type
    return clean, secrets


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Move legacy remote-node credentials into encrypted PostgreSQL storage.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Persist the migration. Without this flag, validate and preview only.",
    )
    args = parser.parse_args()
    result = migrate(apply=bool(args.apply))
    mode = "applied" if result["apply"] else "preview"
    print(
        f"remote-node secret migration {mode}: "
        f"migrated={result['migrated']} skipped={result['skipped']}",
    )


if __name__ == "__main__":
    main()
