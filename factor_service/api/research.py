from __future__ import annotations

from http import HTTPStatus
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from factor_service.clickhouse import settings


router = APIRouter(prefix="/research", tags=["model-research"])


def _internal_base_url() -> str:
    """Return the loopback-only research process URL.

    The public FactorService API is the sole gateway.  Keeping the native-ML
    scheduler on loopback prevents callers from bypassing this service boundary.
    """

    value = str(settings().research_internal_url or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.port is None
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("service.research_internal_url必须是带端口的本机HTTP地址")
    return value


def _decode_response(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {"ok": False, "error": raw.decode("utf-8", errors="replace")[:500]}
    return payload if isinstance(payload, dict) else {"ok": True, "data": payload}


def _proxy(
    method: str,
    path: str,
    *,
    authorization: str = "",
    payload: dict[str, Any] | None = None,
) -> JSONResponse:
    body = None if payload is None else json.dumps(payload, default=str).encode("utf-8")
    headers = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{_internal_base_url()}{path}", data=body, headers=headers, method=method,
    )
    try:
        with urlopen(request, timeout=15) as response:
            return JSONResponse(
                status_code=int(response.status),
                content=_decode_response(response.read()),
            )
    except HTTPError as exc:
        return JSONResponse(status_code=exc.code, content=_decode_response(exc.read()))
    except (URLError, TimeoutError, OSError) as exc:
        return JSONResponse(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            content={"ok": False, "error": f"研究调度进程不可用: {exc}"},
        )


@router.get("/health")
def research_health() -> JSONResponse:
    return _proxy("GET", "/health")


@router.get("/ready")
def research_ready() -> JSONResponse:
    return _proxy("GET", "/ready")


@router.get("/api/v1/status")
def research_status(authorization: str = Header(default="")) -> JSONResponse:
    return _proxy("GET", "/api/v1/status", authorization=authorization)


@router.post("/api/v1/jobs")
def research_job_submit(
    payload: dict[str, Any] = Body(...),
    authorization: str = Header(default=""),
) -> JSONResponse:
    return _proxy("POST", "/api/v1/jobs", authorization=authorization, payload=payload)


__all__ = ["router"]
