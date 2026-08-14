from pathlib import Path

from factor_service.research import config
from factor_service.runtime_config import PROJECT_ROOT


def _runtime(
    *,
    work_root: str = "./custom/research",
    model_artifacts_root: str = "./custom/model-artifacts",
) -> dict:
    return {
        "clickhouse": {
            "host": "10.0.0.8",
            "port": 8124,
            "username": "research",
            "password": "shared-secret",
            "factor_database": "factor_shared",
            "model_database": "model_shared",
        },
        "sources": {"research": {"database": "market_source"}},
        "research": {
            "api_url": "http://10.0.0.9:8001/api/model-research/",
            "token": "worker-secret",
            "storage": {
                "work_root": work_root,
                "model_artifacts_root": model_artifacts_root,
            },
        },
    }


def test_research_settings_are_loaded_from_runtime_yaml(monkeypatch) -> None:
    monkeypatch.setattr(config, "load_runtime_config", lambda: _runtime())

    settings = config.load_settings()

    assert settings.api_url == "http://10.0.0.9:8001/api/model-research"
    assert settings.worker_token == "worker-secret"
    assert settings.clickhouse_host == "10.0.0.8"
    assert settings.clickhouse_port == 8124
    assert settings.clickhouse_user == "research"
    assert settings.clickhouse_password == "shared-secret"
    assert settings.factor_database == "factor_shared"
    assert settings.model_database == "model_shared"
    assert settings.source_database == "market_source"


def test_relative_storage_root_is_resolved_from_project_root(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        config,
        "load_runtime_config",
        lambda: _runtime(work_root="./mounted/model-research"),
    )

    settings = config.load_settings()

    assert settings.work_root == (PROJECT_ROOT / "mounted/model-research").resolve()


def test_absolute_storage_root_is_preserved(monkeypatch, tmp_path) -> None:
    destination = tmp_path / "research-files"
    monkeypatch.setattr(
        config,
        "load_runtime_config",
        lambda: _runtime(work_root=str(destination)),
    )

    settings = config.load_settings()

    assert settings.work_root == destination.resolve()


def test_formal_model_artifact_root_is_independent_from_work_root(monkeypatch, tmp_path) -> None:
    artifact_root = tmp_path / "formal-models"
    monkeypatch.setattr(
        config,
        "load_runtime_config",
        lambda: _runtime(model_artifacts_root=str(artifact_root)),
    )

    settings = config.load_settings()

    assert settings.model_artifacts_root == artifact_root.resolve()
    assert settings.model_artifacts_root != settings.work_root


def test_worker_token_is_optional(monkeypatch) -> None:
    payload = _runtime()
    payload["research"]["token"] = ""
    monkeypatch.setattr(config, "load_runtime_config", lambda: payload)

    settings = config.load_settings()

    assert settings.worker_token == ""
