from __future__ import annotations

import json
import re
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


AUTODL_API_BASE = "https://api.autodl.com"
_INSTANCE_UUID = re.compile(r"^pro-[A-Za-z0-9]{6,64}$")
_IMAGE_NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,80}$")
_RUNNING_STATES = {"running"}


class AutoDLAPIError(RuntimeError):
    """A safe, token-free AutoDL API error."""


def validate_api_token(value: str) -> str:
    token = str(value or "").strip()
    if not 8 <= len(token) <= 4096:
        raise ValueError("AutoDL API Token长度无效")
    if any(ord(character) < 32 or ord(character) == 127 for character in token):
        raise ValueError("AutoDL API Token包含无效控制字符")
    return token


class AutoDLProClient:
    def __init__(
        self,
        instance_uuid: str,
        api_token: str,
        *,
        request_timeout_seconds: int = 30,
    ) -> None:
        self.instance_uuid = validate_instance_uuid(instance_uuid)
        clean_token = str(api_token or "").strip()
        self._api_token = validate_api_token(clean_token) if clean_token else ""
        self.request_timeout_seconds = max(
            5, min(int(request_timeout_seconds), 60),
        )

    def configured(self) -> bool:
        return bool(self._api_token)

    def status(self) -> str:
        return str(self._request(
            "GET", "/api/v1/dev/instance/pro/status",
            {"instance_uuid": self.instance_uuid},
        ) or "unknown").strip().lower()

    def snapshot(self) -> dict[str, Any]:
        data = self._request(
            "GET", "/api/v1/dev/instance/pro/snapshot",
            {"instance_uuid": self.instance_uuid},
        )
        if not isinstance(data, dict):
            raise AutoDLAPIError("AutoDL实例详情返回格式无效")
        return data

    def power_on(self, *, start_command: str = "sleep 1") -> None:
        self._request(
            "POST", "/api/v1/dev/instance/pro/power_on",
            {
                "instance_uuid": self.instance_uuid,
                "payload": "gpu",
                "start_command": str(start_command or "sleep 1")[:200],
            },
        )

    def power_off(self) -> None:
        self._request(
            "POST", "/api/v1/dev/instance/pro/power_off",
            {"instance_uuid": self.instance_uuid},
        )

    def save_image(self, image_name: str) -> dict[str, Any]:
        clean_name = validate_image_name(image_name)
        data = self._request(
            "POST", "/api/v1/dev/instance/pro/image/save",
            {"instance_uuid": self.instance_uuid, "image_name": clean_name},
        )
        if not isinstance(data, dict):
            raise AutoDLAPIError("AutoDL保存镜像返回格式无效")
        return data

    def list_images(
        self, *, page_index: int = 1, page_size: int = 100,
    ) -> dict[str, Any]:
        page = max(1, int(page_index))
        size = max(1, min(int(page_size), 100))
        data = self._request(
            "POST", "/api/v1/dev/instance/pro/image/private/list",
            {"page_index": page, "page_size": size},
        )
        if not isinstance(data, dict):
            raise AutoDLAPIError("AutoDL镜像列表返回格式无效")
        return data

    def wait_for_running(
        self,
        *,
        timeout_seconds: int,
        poll_seconds: float = 5.0,
        checkpoint: Callable[[], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        last_status = "unknown"
        while time.monotonic() < deadline:
            if checkpoint is not None:
                checkpoint()
            last_status = self.status()
            if on_status is not None:
                on_status(last_status)
            if last_status in _RUNNING_STATES:
                return last_status
            time.sleep(max(0.2, min(float(poll_seconds), 30.0)))
        raise TimeoutError(
            f"AutoDL实例等待开机超时，最后状态: {last_status}",
        )

    def _request(
        self, method: str, path: str, payload: dict[str, Any],
    ) -> Any:
        token = self._api_token
        if not token:
            raise ValueError("AutoDL API Token未配置，请在系统设置中填写")
        method_name = str(method or "GET").upper()
        url = f"{AUTODL_API_BASE}{path}"
        body: bytes | None
        if method_name == "GET":
            url = f"{url}?{urlencode(payload)}"
            body = None
        else:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=body,
            method=method_name,
            headers={
                "Authorization": token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "AlphaFactorService/AutoDL-Pro",
            },
        )
        try:
            with urlopen(
                request, timeout=self.request_timeout_seconds,
            ) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = _safe_http_error(exc)
            raise AutoDLAPIError(
                f"AutoDL API请求失败(HTTP {exc.code}){detail}",
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise AutoDLAPIError(f"AutoDL API连接失败: {exc}") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AutoDLAPIError("AutoDL API返回了无效JSON") from exc
        if not isinstance(result, dict):
            raise AutoDLAPIError("AutoDL API返回格式无效")
        if str(result.get("code") or "") != "Success":
            message = str(result.get("msg") or result.get("code") or "未知错误")
            raise AutoDLAPIError(f"AutoDL API拒绝请求: {message[:500]}")
        return result.get("data")


def validate_instance_uuid(value: str) -> str:
    clean = str(value or "").strip()
    if not _INSTANCE_UUID.fullmatch(clean):
        raise ValueError(f"AutoDL Pro实例UUID无效: {clean}")
    return clean


def validate_image_name(value: str) -> str:
    clean = str(value or "").strip()
    if not _IMAGE_NAME.fullmatch(clean):
        raise ValueError("AutoDL镜像名称必须为1到80个可见字符")
    return clean


def sanitize_snapshot(source: dict[str, Any]) -> dict[str, Any]:
    """Return operational fields without SSH/Jupyter credentials."""
    usage = dict(source.get("usage_info") or {})
    return {
        key: source.get(key)
        for key in (
            "region_sign", "payg_price", "origin_pay_price",
            "snapshot_gpu_alias_name", "chip_corp", "cpu_arch",
            "expand_system_disk_size", "system_init_disk_size",
            "proxy_host", "ssh_port",
        )
        if source.get(key) is not None
    } | ({
        "usage_info": {
            key: usage.get(key)
            for key in (
                "valid_at", "cpu_usage_percent", "mem_usage_percent",
                "mem_usage", "mem_limit", "root_fs_used_size",
                "root_fs_total_size", "data_disk_total_size",
                "data_disk_used_size", "pull_image_progress",
                "download_image_progress", "valid",
            )
            if usage.get(key) is not None
        },
    } if usage else {})


def _safe_http_error(exc: HTTPError) -> str:
    try:
        raw = exc.read(2048)
        parsed = json.loads(raw.decode("utf-8"))
        if isinstance(parsed, dict):
            detail = str(parsed.get("msg") or parsed.get("code") or "").strip()
            return f": {detail[:500]}" if detail else ""
    except Exception:
        pass
    return ""


__all__ = [
    "AUTODL_API_BASE", "AutoDLAPIError", "AutoDLProClient", "sanitize_snapshot",
    "validate_api_token", "validate_image_name", "validate_instance_uuid",
]
