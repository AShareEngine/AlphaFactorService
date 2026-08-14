from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from factor_service.research.worker import ResearchWorker


router = APIRouter(prefix="/research", tags=["model-research"])


def _worker(request: Request) -> ResearchWorker:
    worker = getattr(request.app.state, "research_worker", None)
    if worker is None:
        raise RuntimeError("研究调度器尚未启动")
    return worker


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
    scheduler = dict(status.get("scheduler") or {})
    error = str(status.get("last_error") or scheduler.get("last_error") or "")
    return JSONResponse(
        status_code=HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
        content={
            "ok": ready,
            "service": "AlphaFactorServiceResearch",
            "ready": ready,
            "busy": bool(status["busy"]),
            **({"error": error} if error else {}),
        },
    )


@router.get("/status")
def research_status(request: Request) -> JSONResponse:
    try:
        worker = _worker(request)
    except RuntimeError as exc:
        return JSONResponse(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            content={"ok": False, "error": str(exc)},
        )
    return JSONResponse(content=worker.status())


__all__ = ["router"]
