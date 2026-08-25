from __future__ import annotations

import json
from typing import Any

import requests


class JobError(RuntimeError):
    """Base class for errors whose retry semantics are known."""

    retryable = False
    code = "job_error"


class PermanentJobError(JobError):
    retryable = False
    code = "permanent_error"


class RetryableJobError(JobError):
    retryable = True
    code = "retryable_error"


class JobCanceled(PermanentJobError):
    code = "canceled"


class TrainingTimeout(PermanentJobError):
    code = "training_timeout"


class WorkerShutdown(RetryableJobError):
    code = "worker_shutdown"


def classify_exception(exc: BaseException) -> tuple[bool, str]:
    if isinstance(exc, JobError):
        return bool(exc.retryable), str(exc.code)
    if isinstance(exc, (requests.Timeout, requests.ConnectionError, TimeoutError, ConnectionError)):
        return True, "network_error"
    module = type(exc).__module__
    name = type(exc).__name__
    if module.startswith("clickhouse_connect"):
        retryable = name in {"OperationalError", "InterfaceError"}
        return retryable, "clickhouse_transient" if retryable else "clickhouse_query"
    if isinstance(exc, (KeyError, TypeError, ValueError, json.JSONDecodeError)):
        return False, "invalid_job_or_data"
    return False, "unexpected_error"


def error_payload(exc: BaseException) -> dict[str, Any]:
    retryable, code = classify_exception(exc)
    return {"retryable": retryable, "error_code": code, "error_type": type(exc).__name__}


__all__ = [
    "JobCanceled", "JobError", "PermanentJobError", "RetryableJobError",
    "TrainingTimeout", "WorkerShutdown", "classify_exception", "error_payload",
]
