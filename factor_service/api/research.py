from __future__ import annotations

import hmac
from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, Body, Header, Request
from fastapi.responses import JSONResponse

from factor_service.research.errors import PermanentJobError
from factor_service.research.worker import ResearchWorker


router = APIRouter(prefix="/research", tags=["model-research"])


def _worker(request: Request) -> ResearchWorker:
    worker = getattr(request.app.state, "research_worker", None)
    if worker is None:
        raise RuntimeError("研究调度器尚未启动")
    return worker


def _authorize(worker: ResearchWorker, authorization: str) -> JSONResponse | None:
    token = worker.settings.worker_token.strip()
    if not token or hmac.compare_digest(authorization, f"Bearer {token}"):
        return None
    return JSONResponse(
        status_code=HTTPStatus.UNAUTHORIZED,
        content={"ok": False, "error": "研究服务认证失败"},
    )


@router.get("/health")
def research_health(request: Request) -> JSONResponse:
    try:
        _worker(request)
    except RuntimeError as exc:
        return JSONResponse(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            content={"ok": False, "error": str(exc)},
        )
    return JSONResponse(content={"ok": True, "service": "AlphaFactorServiceResearch"})


@router.get("/ready")
def research_ready(request: Request) -> JSONResponse:
    try:
        status = _worker(request).status()
    except RuntimeError as exc:
        return JSONResponse(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            content={"ok": False, "error": str(exc)},
        )
    ready = bool(status["ready"])
    return JSONResponse(
        status_code=HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
        content={
            "ok": ready,
            "service": "AlphaFactorServiceResearch",
            "ready": ready,
            "busy": bool(status["busy"]),
        },
    )


@router.get("/api/v1/status")
def research_status(
    request: Request,
    authorization: str = Header(default=""),
) -> JSONResponse:
    try:
        worker = _worker(request)
    except RuntimeError as exc:
        return JSONResponse(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            content={"ok": False, "error": str(exc)},
        )
    unauthorized = _authorize(worker, authorization)
    return unauthorized or JSONResponse(content=worker.status())


@router.post("/api/v1/jobs")
def research_job_submit(
    request: Request,
    payload: dict[str, Any] = Body(...),
    authorization: str = Header(default=""),
) -> JSONResponse:
    try:
        worker = _worker(request)
    except RuntimeError as exc:
        return JSONResponse(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            content={"ok": False, "error": str(exc)},
        )
    unauthorized = _authorize(worker, authorization)
    if unauthorized is not None:
        return unauthorized
    try:
        result = worker.submit(payload)
    except PermanentJobError as exc:
        return JSONResponse(
            status_code=HTTPStatus.BAD_REQUEST,
            content={"ok": False, "error": str(exc)},
        )
    except RuntimeError as exc:
        return JSONResponse(
            status_code=HTTPStatus.CONFLICT,
            content={"ok": False, "error": str(exc)},
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            status_code=HTTPStatus.BAD_REQUEST,
            content={"ok": False, "error": str(exc)},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            content={"ok": False, "error": str(exc)},
        )
    return JSONResponse(
        status_code=HTTPStatus.ACCEPTED,
        content={"ok": True, "job": result},
    )


__all__ = ["router"]
