from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from factor_service.model_research_repository import (
    ModelResearchConflict,
    ModelResearchRepository,
)
from factor_service.research.dataset_preview import DatasetPreviewService


def _dataset() -> dict:
    return {
        "name": "idempotency",
        "date_start": "2022-01-04",
        "date_end": "2025-12-31",
        "data_cutoff": "2026-01-05T15:30:00+08:00",
        "factors": [{
            "factor_id": "mom_20",
            "factor_version": 2,
            "params_hash": "a" * 64,
            "params": {"window": 20},
        }],
    }


def _job_payload(**updates) -> dict:
    result = {
        "title": "幂等训练",
        "client_study_id": "training-study-1",
        "idempotency_key": "submit-key-1",
        "dataset": _dataset(),
        "model": {"kind": "lightgbm", "params": {"num_leaves": 31}},
        "execution": {"node_id": "local"},
    }
    result.update(updates)
    return result


class _Rows:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Transaction:
    def __init__(self, database: "_MemoryDatabase") -> None:
        self.database = database
        self.snapshot = None

    def __enter__(self):
        self.database.transaction_count += 1
        self.snapshot = (
            deepcopy(self.database.jobs),
            deepcopy(self.database.datasets),
            self.database.insert_count,
            self.database.event_count,
        )
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is not None:
            jobs, datasets, insert_count, event_count = self.snapshot
            self.database.jobs = jobs
            self.database.datasets = datasets
            self.database.insert_count = insert_count
            self.database.event_count = event_count
        return False


class _Connection:
    def __init__(self, database: "_MemoryDatabase") -> None:
        self.database = database

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def transaction(self):
        return _Transaction(self.database)

    def execute(self, query, params=()):
        sql = " ".join(str(query).split())
        if sql.startswith("SELECT to_regclass"):
            return _Rows([{"relation": "model_job_attempts"}])
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            return _Rows()
        if sql.startswith("SELECT * FROM model_jobs WHERE job_id = %s FOR UPDATE"):
            row = self.database.jobs.get(str(params[0]))
            return _Rows([deepcopy(row)] if row else [])
        if sql.startswith("SELECT job_id, config_json FROM model_jobs"):
            if "config_json -> 'submission'" in sql:
                key, _key_again, client_id, _client_again = params
                rows = []
                for job in self.database.jobs.values():
                    submission = dict(job["config_json"].get("submission") or {})
                    if submission.get("scope") != "training":
                        continue
                    if (
                        key and submission.get("idempotency_key") == key
                    ) or (
                        client_id and submission.get("client_study_id") == client_id
                    ):
                        rows.append({
                            "job_id": job["job_id"],
                            "config_json": deepcopy(job["config_json"]),
                        })
                rows.sort(key=lambda row: int(
                    dict(row["config_json"].get("submission") or {}).get("ordinal")
                    or 1
                ))
                return _Rows(rows)
            kind, key, _key_again, client_id, _client_again = params
            rows = []
            for job in self.database.jobs.values():
                preview = dict(job["config_json"].get("preview") or {})
                if job["kind"] != kind:
                    continue
                if (
                    key and preview.get("idempotency_key") == key
                ) or (
                    client_id and preview.get("client_preview_id") == client_id
                ):
                    rows.append({
                        "job_id": job["job_id"],
                        "config_json": deepcopy(job["config_json"]),
                    })
            return _Rows(rows[-1:])
        if sql.startswith("INSERT INTO model_dataset_specs"):
            dataset_id, spec_hash = str(params[0]), str(params[1])
            self.database.datasets.setdefault(spec_hash, dataset_id)
            return _Rows()
        if sql.startswith("SELECT dataset_id FROM model_dataset_specs"):
            spec_hash = str(params[0])
            return _Rows([{"dataset_id": self.database.datasets[spec_hash]}])
        if sql.startswith("SELECT GREATEST"):
            model_id = str(params[0])
            versions = [
                int(job["config_json"].get("planned_model_version") or 0)
                for job in self.database.jobs.values()
                if job["model_id"] == model_id
            ]
            return _Rows([{"version": max(versions, default=0) + 1}])
        if sql.startswith("INSERT INTO model_jobs"):
            self.database.insert_count += 1
            if self.database.fail_on_insert == self.database.insert_count:
                raise RuntimeError("simulated insert failure")
            if "'train'" in sql:
                (
                    job_id, dataset_id, model_id, model_kind, title,
                    config, requested_at, updated_at,
                ) = params
                progress = {}
                kind = "train"
                status = "queued"
            else:
                (
                    job_id, dataset_id, model_id, kind, title, config,
                    progress, requested_at, _started_at, updated_at,
                ) = params
                model_kind = "none"
                status = "running"
            self.database.jobs[str(job_id)] = {
                "job_id": str(job_id),
                "dataset_id": str(dataset_id),
                "model_id": str(model_id),
                "kind": str(kind),
                "model_kind": str(model_kind),
                "title": str(title),
                "status": status,
                "config_json": deepcopy(config.obj),
                "progress_json": deepcopy(getattr(progress, "obj", {})),
                "requested_at": requested_at,
                "updated_at": updated_at,
            }
            return _Rows()
        if sql.startswith("INSERT INTO model_job_events"):
            self.database.event_count += 1
            return _Rows([{"event_id": self.database.event_count}])
        if sql.startswith("SELECT attempt_count FROM model_jobs"):
            row = self.database.jobs.get(str(params[0])) or {}
            return _Rows([{"attempt_count": int(row.get("attempt_count") or 0)}])
        raise AssertionError(sql)


class _MemoryDatabase:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.datasets: dict[str, str] = {}
        self.insert_count = 0
        self.event_count = 0
        self.transaction_count = 0
        self.fail_on_insert: int | None = None

    def connection(self):
        return _Connection(self)

    def job_view(self, job_id: str) -> dict:
        row = deepcopy(self.jobs[job_id])
        config = dict(row["config_json"])
        dataset = dict(config.get("dataset") or {})
        return {
            **row,
            "dataset_hash": str(row["dataset_id"]).removeprefix("dataset_"),
            "dataset_spec": dataset,
            "result_json": deepcopy(row.get("result_json") or {}),
            "error_message": "",
            "model_version": row.get("model_version"),
            "attempt_count": 0,
            "max_attempts": 3,
        }


def _repository(database: _MemoryDatabase) -> ModelResearchRepository:
    repository = ModelResearchRepository(database)
    repository.get_job = database.job_view
    return repository


def test_training_job_replay_returns_original_and_mismatch_conflicts() -> None:
    database = _MemoryDatabase()
    repository = _repository(database)

    first = repository.create_training_job(_job_payload())
    replay = repository.create_training_job(_job_payload())

    assert replay["job_id"] == first["job_id"]
    assert database.insert_count == 1
    assert first["config_json"]["submission"]["request_hash"]

    with pytest.raises(ModelResearchConflict, match="不同训练请求"):
        repository.create_training_job(_job_payload(
            model={"kind": "lightgbm", "params": {"num_leaves": 63}},
        ))
    assert database.insert_count == 1


def test_experiment_create_is_atomic_and_replay_never_duplicates_trials() -> None:
    database = _MemoryDatabase()
    repository = _repository(database)
    payload = {
        **_job_payload(),
        "title": "多模型幂等训练",
        "search": {
            "strategy": "model_ensemble",
            "model_kinds": ["lightgbm", "xgboost"],
            "model_params_by_kind": {"lightgbm": {}, "xgboost": {}},
        },
    }

    first = repository.create_training_experiment(payload)
    assert database.transaction_count == 1
    replay = repository.create_training_experiment(payload)

    assert replay["experiment_id"] == first["experiment_id"]
    assert [job["job_id"] for job in replay["jobs"]] == [
        job["job_id"] for job in first["jobs"]
    ]
    assert database.insert_count == 2
    assert len(database.jobs) == 2

    with pytest.raises(ModelResearchConflict, match="不同训练请求"):
        repository.create_training_experiment({
            **payload,
            "search": {
                **payload["search"],
                "model_kinds": ["lightgbm", "catboost"],
                "model_params_by_kind": {"lightgbm": {}, "catboost": {}},
            },
        })
    assert len(database.jobs) == 2


def test_experiment_failure_rolls_back_all_trials_and_same_request_recovers() -> None:
    database = _MemoryDatabase()
    database.fail_on_insert = 2
    repository = _repository(database)
    payload = {
        **_job_payload(),
        "search": {
            "strategy": "model_ensemble",
            "model_kinds": ["lightgbm", "xgboost"],
            "model_params_by_kind": {"lightgbm": {}, "xgboost": {}},
        },
    }

    with pytest.raises(RuntimeError, match="simulated insert failure"):
        repository.create_training_experiment(payload)
    assert database.transaction_count == 1
    assert database.jobs == {}

    database.fail_on_insert = None
    recovered = repository.create_training_experiment(payload)
    assert recovered["trial_count"] == 2
    assert len(database.jobs) == 2


def test_preview_replay_checks_canonical_request_before_returning_existing() -> None:
    database = _MemoryDatabase()
    repository = _repository(database)
    service = DatasetPreviewService(repository)
    payload = {
        "dataset": _dataset(),
        "split": "train",
        "view": "processed",
        "rows": 25,
        "client_preview_id": "training-preview-1",
        "idempotency_key": "preview-key-1",
    }

    first = service.create(payload)
    replay = service.create(payload)

    assert replay["preview_id"] == first["preview_id"]
    assert database.insert_count == 1

    with pytest.raises(ModelResearchConflict, match="不同Dataset Preview请求"):
        service.create({**payload, "rows": 50})
    assert database.insert_count == 1


def test_registered_candidate_replay_returns_same_model_and_rejects_new_identity() -> None:
    repository = ModelResearchRepository.__new__(ModelResearchRepository)
    registered = {
        "job_id": "job-registered",
        "kind": "train",
        "status": "succeeded",
        "model_id": "public-model",
        "model_version": 7,
        "result_json": {"registration": {"status": "registered"}},
    }
    repository.get_job = lambda _job_id: deepcopy(registered)

    assert repository.register_training_result(
        "job-registered", model_id="public-model",
    )["model_version"] == 7
    with pytest.raises(ModelResearchConflict, match="另一个model_id"):
        repository.register_training_result(
            "job-registered", model_id="different-model",
        )


def test_candidate_rechecks_registration_after_waiting_for_row_lock() -> None:
    database = _MemoryDatabase()
    now = datetime.now(timezone.utc)
    database.jobs["job-raced"] = {
        "job_id": "job-raced",
        "dataset_id": "dataset-a",
        "model_id": "public-model",
        "kind": "train",
        "model_kind": "lightgbm",
        "title": "并发登记",
        "status": "succeeded",
        "config_json": {"planned_model_version": 7},
        "result_json": {"registration": {"status": "registered"}},
        "model_version": 7,
        "requested_at": now,
        "updated_at": now,
    }
    repository = ModelResearchRepository(database)
    calls = 0

    def get_job(job_id):
        nonlocal calls
        calls += 1
        current = database.job_view(job_id)
        if calls == 1:
            current["model_id"] = "temporary-model"
            current["model_version"] = None
        return current

    repository.get_job = get_job
    replay = repository.register_training_result(
        "job-raced", model_id="public-model",
    )

    assert replay["model_id"] == "public-model"
    assert replay["model_version"] == 7
    assert database.insert_count == 0
