from __future__ import annotations

from datetime import datetime
from http import HTTPStatus
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse

from factor_service import model_repository
from factor_service.model_artifacts import ArtifactError
from factor_service.model_backtest import run_model_backtest_job
from factor_service.model_validation import assess_model_validation
from factor_service.model_research_repository import (
    ModelResearchConflict,
    ModelResearchError,
    ModelResearchNotFound,
    ModelResearchRepository,
)
from factor_service.research.worker import ResearchWorker
from factor_service.research.schedule import dispatch_job as dispatch_model_job
from factor_service.research.schedule import run_inference_schedule_tick
from factor_service.schemas import ModelBacktestJobCreate


router = APIRouter(prefix="/model-research", tags=["model-research"])
repository = ModelResearchRepository()


def _worker(request: Request) -> ResearchWorker:
    worker = getattr(request.app.state, "research_worker", None)
    if worker is None:
        raise ModelResearchConflict("研究调度器尚未启动")
    return worker


def _raise(exc: Exception) -> None:
    if isinstance(exc, ModelResearchNotFound):
        status = HTTPStatus.NOT_FOUND
    elif isinstance(exc, ModelResearchConflict):
        status = HTTPStatus.CONFLICT
    elif isinstance(exc, (ArtifactError, FileNotFoundError)):
        status = HTTPStatus.NOT_FOUND
    elif isinstance(exc, (ModelResearchError, TypeError, ValueError)):
        status = HTTPStatus.BAD_REQUEST
    else:
        status = HTTPStatus.INTERNAL_SERVER_ERROR
    raise HTTPException(status_code=int(status), detail=str(exc)) from exc


def _dispatch(request: Request, job: dict[str, Any]) -> tuple[dict[str, Any], int]:
    return dispatch_model_job(repository, _worker(request), job)


def _model_validation_view(
    model: dict[str, Any], backtest: Any | None,
) -> dict[str, Any]:
    assessed = assess_model_validation(model.get("metrics_json"), backtest)
    stored = dict((model.get("manifest_json") or {}).get("validation") or {})
    if (
        stored.get("manual_override") is True
        and str(stored.get("backtest_job_id") or "")
        == str(assessed.get("backtest_job_id") or "")
    ):
        return stored
    return assessed


def _model_response(model: dict[str, Any], backtest: Any | None) -> dict[str, Any]:
    result = dict(model)
    validation = _model_validation_view(model, backtest)
    result["registry_state"] = str(model.get("state") or "candidate")
    result["state"] = "validated" if validation.get("approved") is True else "candidate"
    result["latest_backtest"] = backtest.model_dump() if backtest is not None else None
    result["validation"] = validation
    return result


@router.get("/jobs")
def list_jobs(
    status: str = Query(default=""), limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    try:
        return {"ok": True, "jobs": repository.list_jobs(status=status, limit=limit)}
    except Exception as exc:
        _raise(exc)


@router.post("/jobs", status_code=HTTPStatus.CREATED)
def create_job(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return {"ok": True, "job": repository.create_training_job(payload)}
    except Exception as exc:
        _raise(exc)


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    try:
        return {"ok": True, "job": repository.get_job(job_id)}
    except Exception as exc:
        _raise(exc)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    try:
        return {"ok": True, "job": repository.cancel_job(job_id)}
    except Exception as exc:
        _raise(exc)


@router.post("/jobs/{job_id}/dispatch")
def dispatch_job(request: Request, job_id: str) -> JSONResponse:
    try:
        payload, status = _dispatch(request, repository.get_job(job_id))
        return JSONResponse(
            status_code=status,
            content=jsonable_encoder(payload),
        )
    except Exception as exc:
        _raise(exc)


@router.get("/jobs/{job_id}/events")
def list_events(
    job_id: str, after: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    try:
        return {"ok": True, "events": repository.list_events(job_id, after=after)}
    except Exception as exc:
        _raise(exc)


@router.get("/jobs/{job_id}/artifacts")
def list_artifacts(job_id: str) -> dict[str, Any]:
    try:
        return {"ok": True, "artifacts": repository.list_artifacts(job_id)}
    except Exception as exc:
        _raise(exc)


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(request: Request, artifact_id: str) -> FileResponse:
    try:
        artifact = repository.get_artifact(artifact_id)
        path = _worker(request).artifact_store.resolve(str(artifact["relative_path"]))
        return FileResponse(path, media_type="application/octet-stream", filename=path.name)
    except Exception as exc:
        _raise(exc)


@router.get("/models")
def list_models(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    try:
        models = repository.list_models(limit=limit)
        backtests = model_repository.latest_model_backtests([
            (str(model["model_id"]), int(model["version"])) for model in models
        ])
        return {
            "ok": True,
            "models": [
                _model_response(
                    model,
                    backtests.get((str(model["model_id"]), int(model["version"]))),
                )
                for model in models
            ],
        }
    except Exception as exc:
        _raise(exc)


@router.get("/models/{model_id}/versions/{version}")
def get_model(model_id: str, version: int) -> dict[str, Any]:
    try:
        model = repository.get_model(model_id, version)
        backtest = model_repository.latest_model_backtests([(model_id, version)]).get(
            (model_id, version)
        )
        return {"ok": True, "model": _model_response(model, backtest)}
    except Exception as exc:
        _raise(exc)


@router.get("/models/{model_id}/versions/{version}/strategy-deployments/{mode}")
def get_strategy_deployment(model_id: str, version: int, mode: str) -> dict[str, Any]:
    try:
        repository.get_model(model_id, version)
        return {
            "ok": True,
            "deployment": repository.get_strategy_deployment(
                model_id, version, mode=mode,
            ),
        }
    except Exception as exc:
        _raise(exc)


@router.post("/models/{model_id}/versions/{version}/strategy-deployments/{mode}")
def record_strategy_deployment(
    model_id: str,
    version: int,
    mode: str,
    payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    try:
        model = repository.get_model(model_id, version)
        backtest = model_repository.latest_model_backtests([(model_id, version)]).get(
            (model_id, version)
        )
        if _model_validation_view(model, backtest).get("approved") is not True:
            raise ModelResearchConflict("模型尚未通过研究门槛，不能加入策略运行")
        return {
            "ok": True,
            "deployment": repository.record_strategy_deployment(
                model_id,
                version,
                mode=mode,
                state=str(payload.get("state") or "active"),
                snapshot=dict(payload.get("snapshot") or {}),
            ),
        }
    except Exception as exc:
        _raise(exc)


def _inference_availability(
    model: dict[str, Any], *, trade_date: str = "", data_cutoff: str = "",
) -> dict[str, Any]:
    requested_date = None
    if trade_date:
        requested_date = datetime.fromisoformat(trade_date).date()
    cutoff = None
    if data_cutoff:
        cutoff = datetime.fromisoformat(data_cutoff.replace("Z", "+00:00"))
    return model_repository.model_inference_availability(
        factors=list((model.get("dataset_spec") or {}).get("factors") or []),
        requested_trade_date=requested_date,
        data_cutoff=cutoff,
    )


@router.get("/models/{model_id}/versions/{version}/inference-availability")
def inference_availability(model_id: str, version: int) -> dict[str, Any]:
    try:
        model = repository.get_model(model_id, version)
        return {"ok": True, "availability": _inference_availability(model)}
    except Exception as exc:
        _raise(exc)


@router.post("/models/{model_id}/versions/{version}/inferences")
def create_inference(
    request: Request,
    model_id: str,
    version: int,
    payload: dict[str, Any] = Body(default={}),
) -> JSONResponse:
    try:
        model = repository.get_model(model_id, version)
        trade_date = str(payload.get("trade_date") or "")
        data_cutoff = str(payload.get("data_cutoff") or "")
        availability = _inference_availability(
            model, trade_date=trade_date, data_cutoff=data_cutoff,
        )
        trade_date = trade_date or str(availability.get("trade_date") or "")
        if not trade_date:
            raise ModelResearchConflict("模型因子与中证500行情没有共同可推理交易日")
        if trade_date > str(availability.get("trade_date") or ""):
            raise ModelResearchError(
                f"目标日{trade_date}尚不可推理，当前共同最新交易日为{availability.get('trade_date')}"
            )
        if not str(payload.get("trade_date") or ""):
            availability = _inference_availability(
                model, trade_date=trade_date, data_cutoff=data_cutoff,
            )
        if availability.get("requested_trade_date_available") is not True:
            raise ModelResearchError(f"目标日{trade_date}不是完整可推理交易日")
        job = repository.create_inference_job(
            model_id,
            version,
            {**payload, "trade_date": trade_date},
        )
        if str(job.get("status")) == "queued":
            result, status = _dispatch(request, job)
            result["availability"] = availability
            return JSONResponse(status_code=status, content=jsonable_encoder(result))
        status = HTTPStatus.OK if str(job.get("status")) == "succeeded" else HTTPStatus.ACCEPTED
        return JSONResponse(
            status_code=int(status),
            content=jsonable_encoder({
                "ok": True,
                "job": job,
                "availability": availability,
            }),
        )
    except Exception as exc:
        _raise(exc)


@router.get("/predictions")
def list_predictions(
    model_id: str,
    model_version: int,
    trade_date: str = "",
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    try:
        parsed_date = datetime.fromisoformat(trade_date).date() if trade_date else None
        rows = model_repository.list_model_predictions(
            model_id=model_id,
            model_version=model_version,
            trade_date=parsed_date,
            limit=limit,
        )
        return {"ok": True, "predictions": rows}
    except Exception as exc:
        _raise(exc)


@router.get("/models/{model_id}/versions/{version}/signals")
def list_signals(
    model_id: str,
    version: int,
    trade_date: str,
    top_n: int = Query(default=20, ge=1, le=500),
) -> dict[str, Any]:
    try:
        model = repository.get_model(model_id, version)
        backtest = model_repository.latest_model_backtests([(model_id, version)]).get(
            (model_id, version)
        )
        if _model_validation_view(model, backtest).get("approved") is not True:
            raise ModelResearchConflict("模型尚未通过研究门槛，不能用于正式策略信号")
        rows = model_repository.list_model_signals(
            model_id=model_id,
            model_version=version,
            trade_date=datetime.fromisoformat(trade_date).date(),
            top_n=top_n,
        )
        return {"ok": True, "signals": rows}
    except Exception as exc:
        _raise(exc)


@router.post("/models/{model_id}/versions/{version}/backtests", status_code=HTTPStatus.CREATED)
def create_backtest(
    background_tasks: BackgroundTasks,
    model_id: str,
    version: int,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    try:
        repository.get_model(model_id, version)
        created = model_repository.create_model_backtest_job(ModelBacktestJobCreate(
            model_id=model_id,
            model_version=version,
            universe_id="csi500",
            top_n=20,
            rebalance_every=5,
            date_preset=str(payload.get("date_preset") or "3y"),
        ))
        background_tasks.add_task(run_model_backtest_job, created.backtest_job_id)
        return {"ok": True, "backtest": created}
    except Exception as exc:
        _raise(exc)


@router.get("/model-backtests/{backtest_job_id}")
def get_backtest(backtest_job_id: str) -> dict[str, Any]:
    try:
        result = model_repository.get_model_backtest_job(backtest_job_id)
        if result is None:
            raise ModelResearchNotFound("模型回测任务不存在")
        if result.status == "success":
            model = repository.get_model(result.model_id, result.model_version)
            validation = _model_validation_view(model, result)
            stored = dict((model.get("manifest_json") or {}).get("validation") or {})
            already_recorded = (
                str(stored.get("backtest_job_id") or "") == backtest_job_id
                and bool(stored.get("manual_override")) == bool(validation.get("manual_override"))
                and bool(stored.get("passed")) == bool(validation.get("passed"))
            )
            if not already_recorded:
                repository.record_validation_result(
                    result.model_id,
                    result.model_version,
                    backtest_job_id,
                    approved=bool(validation.get("passed")),
                    validation=validation,
                )
        else:
            validation = None
        payload = result.model_dump()
        payload["validation"] = validation
        return {"ok": True, "backtest": payload}
    except Exception as exc:
        _raise(exc)


@router.get("/model-backtests/{backtest_job_id}/daily")
def get_backtest_daily(
    backtest_job_id: str, limit: int = Query(default=5000, ge=1, le=20000),
) -> dict[str, Any]:
    try:
        if model_repository.get_model_backtest_job(backtest_job_id) is None:
            raise ModelResearchNotFound("模型回测任务不存在")
        return {
            "ok": True,
            "daily": model_repository.list_model_backtest_daily(backtest_job_id, limit=limit),
        }
    except Exception as exc:
        _raise(exc)


@router.post("/models/{model_id}/versions/{version}/backtests/{backtest_job_id}/validate")
def validate_backtest(
    model_id: str,
    version: int,
    backtest_job_id: str,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    try:
        result = model_repository.get_model_backtest_job(backtest_job_id)
        if result is None or result.status != "success":
            raise ModelResearchConflict("模型回测尚未成功，不能标记为已验证")
        source = repository.get_model(model_id, version)
        validation = assess_model_validation(source.get("metrics_json"), result)
        override = bool(payload.get("override", False))
        reason = str(payload.get("reason") or "").strip()
        if validation["passed"] is not True and not override:
            labels = [
                str(item["label"])
                for item in validation["checks"]
                if item["passed"] is not True
            ]
            raise ModelResearchConflict("模型未通过研究门槛：" + "、".join(labels))
        if override and not reason:
            raise ModelResearchError("人工放行必须填写原因")
        validation.update({
            "approved": True,
            "manual_override": override,
            "override_reason": reason if override else "",
        })
        model = repository.mark_validated(
            model_id, version, backtest_job_id, validation=validation,
        )
        return {"ok": True, "model": model, "backtest": result, "validation": validation}
    except Exception as exc:
        _raise(exc)


@router.get("/inference-schedules")
def list_inference_schedules() -> dict[str, Any]:
    try:
        return {"ok": True, "schedules": repository.list_inference_schedules()}
    except Exception as exc:
        _raise(exc)


@router.put("/models/{model_id}/versions/{version}/inference-schedule")
def update_inference_schedule(
    model_id: str, version: int, payload: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    try:
        repository.get_model(model_id, version)
        return {
            "ok": True,
            "schedule": repository.update_inference_schedule(model_id, version, payload),
        }
    except Exception as exc:
        _raise(exc)


@router.post("/inference-scheduler/tick")
def inference_scheduler_tick(
    request: Request, payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            **run_inference_schedule_tick(
                repository,
                _worker(request),
                force=bool(payload.get("force", False)),
            ),
        }
    except Exception as exc:
        _raise(exc)


__all__ = ["router"]
