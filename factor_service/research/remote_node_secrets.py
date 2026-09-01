from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


REMOTE_NODE_SECRET_KEY_ENV = "ALPHA_REMOTE_NODE_SECRET_KEY"
_ENVELOPE_VERSION = 1
_NONCE_BYTES = 12
_SECRET_FIELDS = {"api_token", "ssh_password", "ssh_private_key"}
_SECRET_LIMITS = {
    "api_token": 4096,
    "ssh_password": 4096,
    "ssh_private_key": 131_072,
}


class RemoteNodeSecretKeyError(RuntimeError):
    """The process cannot safely encrypt or decrypt remote-node secrets."""


class RemoteNodeSecretCipher:
    """AES-GCM envelope encryption for one node's PostgreSQL secret payload."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise RemoteNodeSecretKeyError("远程节点加密主密钥必须是32字节")
        self._cipher = AESGCM(key)

    @classmethod
    def from_environment(cls) -> "RemoteNodeSecretCipher":
        encoded = str(os.environ.get(REMOTE_NODE_SECRET_KEY_ENV) or "").strip()
        if not encoded:
            raise RemoteNodeSecretKeyError(
                f"进程环境缺少{REMOTE_NODE_SECRET_KEY_ENV}，无法读写远程节点秘密",
            )
        try:
            key = base64.b64decode(encoded, altchars=b"-_", validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RemoteNodeSecretKeyError(
                f"{REMOTE_NODE_SECRET_KEY_ENV}必须是32字节密钥的Base64编码",
            ) from exc
        return cls(key)

    def encrypt(self, node_id: str, payload: Mapping[str, Any]) -> bytes:
        clean = _secret_payload(payload)
        plaintext = json.dumps(
            clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(nonce, plaintext, _aad(node_id))
        return bytes([_ENVELOPE_VERSION]) + nonce + ciphertext

    def decrypt(self, node_id: str, envelope: bytes | memoryview) -> dict[str, str]:
        value = bytes(envelope)
        if len(value) <= 1 + _NONCE_BYTES + 16:
            raise ValueError("PostgreSQL远程节点秘密密文无效")
        if value[0] != _ENVELOPE_VERSION:
            raise ValueError(f"不支持的远程节点秘密密文版本: {value[0]}")
        nonce = value[1:1 + _NONCE_BYTES]
        ciphertext = value[1 + _NONCE_BYTES:]
        try:
            plaintext = self._cipher.decrypt(nonce, ciphertext, _aad(node_id))
        except InvalidTag as exc:
            raise RemoteNodeSecretKeyError(
                "远程节点秘密无法解密，请核对加密主密钥是否一致",
            ) from exc
        try:
            payload = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("PostgreSQL远程节点秘密明文格式无效") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("PostgreSQL远程节点秘密必须是对象")
        return _secret_payload(payload)


def _aad(node_id: str) -> bytes:
    clean = str(node_id or "").strip()
    if not clean:
        raise ValueError("远程训练节点ID不能为空")
    return f"alphablocks:model-execution-node:{clean}:v1".encode("utf-8")


def _secret_payload(payload: Mapping[str, Any]) -> dict[str, str]:
    unknown = set(payload) - _SECRET_FIELDS
    if unknown:
        raise ValueError("远程节点秘密包含不支持字段: " + ", ".join(sorted(unknown)))
    clean: dict[str, str] = {}
    for field in sorted(_SECRET_FIELDS):
        value = str(payload.get(field) or "")
        if len(value.encode("utf-8")) > _SECRET_LIMITS[field]:
            raise ValueError(f"远程节点秘密字段过长: {field}")
        if value:
            clean[field] = value
    return clean


__all__ = [
    "REMOTE_NODE_SECRET_KEY_ENV",
    "RemoteNodeSecretCipher",
    "RemoteNodeSecretKeyError",
]
