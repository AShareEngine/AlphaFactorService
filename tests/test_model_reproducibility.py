from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from factor_service import model_repository
from factor_service.model_reproducibility import (
    build_model_reproducibility_audit,
    compare_reproducibility_metrics,
)


def _model(model_id: str, version: int, job_id: str, *, replay: bool = False):
    config = {}
    if replay:
        config["research_origin"] = {
            "mode": "exact_replay",
            "source_job_id": "source_job",
            "source_model_id": "source_model",
            "source_model_version": 1,
            "source_dataset_hash": "dataset_hash",
            "source_config_hash": "config_hash",
        }
    return {
        "model_id": model_id,
        "version": version,
        "job_id": job_id,
        "dataset_hash": "dataset_hash",
        "job_config_json": config,
        "metrics_json": {
            "validation": {"rank_ic": 0.041, "ic_ir": 0.62, "days": 120},
            "test": {"rank_ic": 0.038, "rmse": 0.74, "days": 118},
        },
    }


def test_metric_audit_accepts_tiny_float_noise():
    source = {"validation": {"rank_ic": 0.04, "days": 120}}
    replay = {"validation": {"rank_ic": 0.04000000002, "days": 120}}

    audit = compare_reproducibility_metrics(source, replay)

    assert audit["status"] == "equivalent"
    assert audit["passed"] is True
    assert audit["failed_count"] == 0


def test_metric_audit_reports_missing_or_drifted_values():
    source = {"validation": {"rank_ic": 0.04, "ic_ir": 0.5}}
    replay = {"validation": {"rank_ic": 0.01}}

    audit = compare_reproducibility_metrics(source, replay)

    assert audit["status"] == "drifted"
    assert audit["passed"] is False
    assert {item["path"] for item in audit["differences"]} == {
        "validation.ic_ir", "validation.rank_ic",
    }


def test_prediction_audit_requires_identical_keys_and_values(monkeypatch):
    rows = [
        ("source_model", 1, date(2026, 8, 10), "000001.SZ", 0.4, 1.0),
        ("replay_model", 1, date(2026, 8, 10), "000001.SZ", 0.4, 1.0),
        ("source_model", 1, date(2026, 8, 10), "000002.SZ", -0.2, -1.0),
        ("replay_model", 1, date(2026, 8, 10), "000002.SZ", -0.2, -1.0),
    ]

    class FakeClient:
        def query(self, _sql, parameters):
            assert parameters["source_count"] == 2
            return SimpleNamespace(result_rows=rows)

    monkeypatch.setattr(model_repository, "client", lambda: FakeClient())
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="alpha_models"),
    )

    audit = model_repository.model_prediction_reproducibility_audit(
        source_model_id="source_model", source_model_version=1,
        replay_model_id="replay_model", replay_model_version=1,
    )

    assert audit["status"] == "exact"
    assert audit["passed"] is True
    assert audit["key_set_equal"] is True
    assert audit["common_rows"] == 2
    assert audit["raw_prediction"]["max_absolute_delta"] == 0.0


def test_prediction_audit_detects_missing_rows(monkeypatch):
    rows = [
        ("source_model", 1, date(2026, 8, 10), "000001.SZ", 0.4, 1.0),
        ("replay_model", 1, date(2026, 8, 10), "000001.SZ", 0.4, 1.0),
        ("source_model", 1, date(2026, 8, 10), "000002.SZ", -0.2, -1.0),
    ]

    class FakeClient:
        def query(self, _sql, parameters):
            return SimpleNamespace(result_rows=rows)

    monkeypatch.setattr(model_repository, "client", lambda: FakeClient())
    monkeypatch.setattr(
        model_repository, "settings",
        lambda: SimpleNamespace(model_database="alpha_models"),
    )

    audit = model_repository.model_prediction_reproducibility_audit(
        source_model_id="source_model", source_model_version=1,
        replay_model_id="replay_model", replay_model_version=1,
    )

    assert audit["status"] == "drifted"
    assert audit["source_only_rows"] == 1
    assert audit["key_set_equal"] is False


def test_full_audit_is_exact_when_control_metrics_and_predictions_match():
    source = _model("source_model", 1, "source_job")
    replay = _model("replay_model", 1, "replay_job", replay=True)
    prediction_audit = {
        "status": "exact", "passed": True, "key_set_equal": True,
        "common_rows": 1000,
    }

    audit = build_model_reproducibility_audit(source, replay, prediction_audit)

    assert audit["status"] == "exact"
    assert audit["passed"] is True
    assert all(item["passed"] for item in audit["configuration_checks"])


def test_full_audit_surfaces_prediction_drift():
    source = _model("source_model", 1, "source_job")
    replay = _model("replay_model", 1, "replay_job", replay=True)

    audit = build_model_reproducibility_audit(
        source, replay,
        {"status": "drifted", "passed": False, "key_set_equal": True},
    )

    assert audit["status"] == "drifted"
    assert audit["passed"] is False
