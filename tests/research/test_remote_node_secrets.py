from __future__ import annotations

import base64

import pytest

from factor_service.research.remote_node_secrets import (
    REMOTE_NODE_SECRET_KEY_ENV,
    RemoteNodeSecretCipher,
    RemoteNodeSecretKeyError,
)
from scripts import migrate_remote_node_secrets_to_postgresql as migration


def test_remote_node_secret_cipher_round_trip_and_binds_node_id() -> None:
    cipher = RemoteNodeSecretCipher(bytes(range(32)))
    secrets = {
        "ssh_password": "password-value",
        "api_token": "token-value",
        "ssh_private_key": "private-key-value",
    }

    encrypted = cipher.encrypt("autodl-gpu-01", secrets)

    assert encrypted != repr(secrets).encode()
    assert b"password-value" not in encrypted
    assert cipher.decrypt("autodl-gpu-01", encrypted) == secrets
    with pytest.raises(RemoteNodeSecretKeyError, match="无法解密"):
        cipher.decrypt("autodl-gpu-02", encrypted)


def test_remote_node_secret_cipher_loads_base64_master_key(monkeypatch) -> None:
    key = bytes(reversed(range(32)))
    monkeypatch.setenv(
        REMOTE_NODE_SECRET_KEY_ENV,
        base64.urlsafe_b64encode(key).decode("ascii"),
    )

    cipher = RemoteNodeSecretCipher.from_environment()
    encrypted = cipher.encrypt("node-1", {"ssh_password": "secret"})

    assert cipher.decrypt("node-1", encrypted) == {"ssh_password": "secret"}


@pytest.mark.parametrize("value", ["", "not-base64", base64.b64encode(b"short").decode()])
def test_remote_node_secret_cipher_rejects_missing_or_invalid_key(
    monkeypatch, value: str,
) -> None:
    if value:
        monkeypatch.setenv(REMOTE_NODE_SECRET_KEY_ENV, value)
    else:
        monkeypatch.delenv(REMOTE_NODE_SECRET_KEY_ENV, raising=False)

    with pytest.raises(RemoteNodeSecretKeyError):
        RemoteNodeSecretCipher.from_environment()


def test_legacy_remote_node_secret_migration_scrubs_config_references(
    tmp_path, monkeypatch,
) -> None:
    key = tmp_path / "remote_ed25519"
    token = tmp_path / "autodl_api_token"
    key.write_text("private-key-content", encoding="utf-8")
    token.write_text("api-token-content", encoding="utf-8")
    monkeypatch.setenv("ALPHA_AUTODL_API_TOKEN_FILE", str(token))

    clean, secrets = migration.legacy_node_payload("node-1", {
        "id": "node-1",
        "ssh_key": str(key),
        "password_env": "",
        "lifecycle_provider": "autodl_pro",
        "api_token_env": "ALPHA_AUTODL_API_TOKEN",
    })

    assert clean["authentication_type"] == "ssh_private_key"
    assert not ({"ssh_key", "password_env", "api_token_env"} & set(clean))
    assert secrets == {
        "ssh_private_key": "private-key-content",
        "ssh_password": "",
        "api_token": "api-token-content",
    }
