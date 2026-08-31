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
            "scheduler": {"enabled": True, "refresh_seconds": 45},
            "storage": {
                "work_root": work_root,
                "model_artifacts_root": model_artifacts_root,
                "dataset_cache_retention_hours": 24,
                "dataset_cache_cleanup_interval_seconds": 1800,
            },
        },
    }


def test_research_settings_are_loaded_from_runtime_yaml(monkeypatch) -> None:
    monkeypatch.setattr(config, "load_runtime_config", lambda: _runtime())

    settings = config.load_settings()

    assert settings.clickhouse_host == "10.0.0.8"
    assert settings.clickhouse_port == 8124
    assert settings.clickhouse_user == "research"
    assert settings.clickhouse_password == "shared-secret"
    assert settings.factor_database == "factor_shared"
    assert settings.model_database == "model_shared"
    assert settings.source_database == "market_source"
    assert settings.scheduler_enabled is True
    assert settings.scheduler_refresh_seconds == 45
    assert settings.dataset_cache_retention_hours == 24
    assert settings.dataset_cache_cleanup_interval_seconds == 1800


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


def test_model_object_store_loads_credentials_from_protected_file(monkeypatch, tmp_path) -> None:
    credentials = tmp_path / "minio.env"
    credentials.write_text(
        "MINIO_APP_ACCESS_KEY=app-user\n"
        "MINIO_APP_SECRET_KEY=app-secret\n"
        "S3_ENDPOINT=http://10.126.126.5:9000\n"
        "S3_BUCKET=alphablocks-models\n"
        "S3_REGION=us-east-1\n",
        encoding="utf-8",
    )
    runtime = _runtime()
    runtime["research"]["storage"]["object_store"] = {
        "enabled": True,
        "credentials_file": str(credentials),
        "prefix": "final-models",
        "artifact_kinds": ["bundle"],
    }
    monkeypatch.setattr(config, "load_runtime_config", lambda: runtime)

    settings = config.load_settings()

    assert settings.model_object_store.enabled is True
    assert settings.model_object_store.endpoint_url == "http://10.126.126.5:9000"
    assert settings.model_object_store.bucket == "alphablocks-models"
    assert settings.model_object_store.access_key == "app-user"
    assert settings.model_object_store.secret_key == "app-secret"
    assert settings.model_object_store.prefix == "final-models"
    assert settings.model_object_store.artifact_kinds == ("bundle",)
