from pathlib import Path

from factor_service.research import config
from factor_service.runtime_config import PROJECT_ROOT


def _runtime(*, storage_root: str = "./custom/research") -> dict:
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
            "listen_host": "0.0.0.0",
            "listen_port": 8788,
            "storage": {"root": storage_root},
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
    assert settings.service_host == "0.0.0.0"
    assert settings.service_port == 8788


def test_relative_storage_root_is_resolved_from_project_root(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        config,
        "load_runtime_config",
        lambda: _runtime(storage_root="./mounted/model-research"),
    )

    settings = config.load_settings()

    assert settings.work_root == (PROJECT_ROOT / "mounted/model-research").resolve()


def test_absolute_storage_root_is_preserved(monkeypatch, tmp_path) -> None:
    destination = tmp_path / "research-files"
    monkeypatch.setattr(
        config,
        "load_runtime_config",
        lambda: _runtime(storage_root=str(destination)),
    )

    settings = config.load_settings()

    assert settings.work_root == destination.resolve()


def test_worker_token_is_optional(monkeypatch) -> None:
    payload = _runtime()
    payload["research"]["token"] = ""
    monkeypatch.setattr(config, "load_runtime_config", lambda: payload)

    settings = config.load_settings()

    assert settings.worker_token == ""


def test_service_port_is_validated(monkeypatch) -> None:
    payload = _runtime()
    payload["research"]["listen_port"] = 70000
    monkeypatch.setattr(config, "load_runtime_config", lambda: payload)

    try:
        config.load_settings()
    except ValueError as exc:
        assert "research.listen_port" in str(exc)
    else:
        raise AssertionError("invalid port must fail")
