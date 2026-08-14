from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from factor_service import model_repository
from factor_service.model_research_repository import (
    ModelResearchConflict,
    ModelResearchRepository,
)


def dispatch_job(
    repository: ModelResearchRepository,
    scheduler: Any,
    job: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Lease and submit one job inside the unified FactorService process."""
    status = str(job.get("status") or "")
    if status in {"leased", "running", "uploading"}:
        return {"ok": True, "job": job, "service": {"accepted": True}}, 202
    if status == "succeeded":
        return {"ok": True, "job": job, "service": {"accepted": False}}, 200
    if status != "queued":
        raise ModelResearchConflict(f"任务状态{status or '未知'}不可调度")
    leased = repository.claim_specific_job(str(job["job_id"]), lease_seconds=90)
    lease_token = str(leased.get("lease_token") or "")
    try:
        accepted = scheduler.submit(leased)
    except Exception as exc:
        current = repository.get_job(str(job["job_id"]))
        if str(current.get("status")) == "leased":
            repository.release_dispatch_lease(
                str(job["job_id"]),
                lease_token=lease_token,
                error_message=f"模型任务启动失败: {exc}",
            )
        raise
    return {
        "ok": True,
        "job": repository.get_job(str(job["job_id"])),
        "service": accepted,
    }, 202


def run_inference_schedule_tick(
    repository: ModelResearchRepository,
    scheduler: Any,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    submitted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    schedules = repository.list_inference_schedules()
    for schedule in schedules:
        model_id = str(schedule["model_id"])
        version = int(schedule["model_version"])
        if schedule.get("enabled") is not True or str(schedule.get("state")) != "validated":
            skipped.append({"model_id": model_id, "version": version, "reason": "disabled_or_unvalidated"})
            continue
        run_after = str(schedule.get("run_after_local") or "16:30")[:5]
        if not force and current.strftime("%H:%M") < run_after:
            skipped.append({"model_id": model_id, "version": version, "reason": "before_run_time"})
            continue
        prediction = dict(schedule.get("prediction_json") or {})
        after_date = str(
            schedule.get("last_submitted_trade_date")
            or prediction.get("latest_trade_date")
            or prediction.get("date_end")
            or "1990-01-01"
        )[:10]
        dates = model_repository.model_inference_dates(
            factors=list((schedule.get("dataset_spec") or {}).get("factors") or []),
            after_date=datetime.fromisoformat(after_date).date(),
            before_date=current.date(),
            data_cutoff=current,
            limit=int(schedule.get("max_catchup_days") or 20),
        )
        if not dates:
            repository.record_inference_schedule_tick(model_id, version)
            skipped.append({"model_id": model_id, "version": version, "reason": "up_to_date"})
            continue
        trade_date = str(dates[0])[:10]
        job = repository.create_inference_job(
            model_id,
            version,
            {"trade_date": trade_date, "data_cutoff": current.isoformat()},
        )
        dispatched = False
        if str(job.get("status")) == "queued":
            try:
                dispatch_job(repository, scheduler, job)
                dispatched = True
            except Exception as exc:
                repository.record_inference_schedule_tick(model_id, version, error=str(exc))
                skipped.append({
                    "model_id": model_id,
                    "version": version,
                    "trade_date": trade_date,
                    "reason": "research_service_busy",
                })
                continue
        repository.record_inference_schedule_tick(model_id, version, trade_date=trade_date)
        submitted.append({
            "model_id": model_id,
            "version": version,
            "trade_date": trade_date,
            "job_id": job["job_id"],
            "dispatched": dispatched,
        })
    return {
        "checked": len(schedules),
        "submitted": submitted,
        "skipped": skipped,
    }


__all__ = ["dispatch_job", "run_inference_schedule_tick"]
