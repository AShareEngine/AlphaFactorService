from pathlib import Path

import yaml

from factor_service import config
from factor_service import runtime_config


def test_runtime_yaml_loads_factor_service_settings(monkeypatch) -> None:
    payload = {
        "service": {
            "host": "0.0.0.0",
            "port": 8110,
            "cors_origins": ["http://127.0.0.1:3000"],
        },
        "clickhouse": {
            "host": "10.126.126.3",
            "port": 8123,
            "username": "factor",
            "password": "secret",
            "secure": True,
            "factor_database": "factors",
            "model_database": "models",
        },
        "sources": {
            "factor": {
                "database": "source",
                "stock_daily_table": "daily",
                "stock_code_column": "symbol",
                "stock_date_column": "date",
                "stock_price_column": "close_price",
                "stock_basic_table": "basic",
                "stock_basic_type_column": "asset_type",
                "stock_basic_stock_type_value": "stock",
            }
        },
    }
    monkeypatch.setattr(config, "load_runtime_config", lambda: payload)

    settings = config.load_settings()

    assert settings.host == "0.0.0.0"
    assert settings.port == 8110
    assert settings.cors_origins == ("http://127.0.0.1:3000",)
    assert settings.clickhouse_host == "10.126.126.3"
    assert settings.clickhouse_user == "factor"
    assert settings.clickhouse_password == "secret"
    assert settings.clickhouse_secure is True
    assert settings.clickhouse_database == "factors"
    assert settings.model_database == "models"
    assert settings.source_database == "source"
    assert settings.stock_daily_table == "daily"


def test_runtime_config_path_does_not_depend_on_working_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALPHA_FACTOR_RUNTIME_CONFIG", raising=False)

    assert runtime_config.runtime_config_path() == (
        runtime_config.PROJECT_ROOT / "config/runtime.local.yaml"
    ).resolve()


def test_runtime_config_path_can_be_explicitly_selected(monkeypatch, tmp_path) -> None:
    selected = tmp_path / "runtime.custom.yaml"
    selected.write_text(yaml.safe_dump({"service": {"port": 8120}}), encoding="utf-8")
    monkeypatch.setenv("ALPHA_FACTOR_RUNTIME_CONFIG", str(selected))

    assert runtime_config.load_runtime_config() == {"service": {"port": 8120}}


def test_invalid_yaml_root_is_rejected(tmp_path) -> None:
    selected = tmp_path / "runtime.yaml"
    selected.write_text("- not\n- an\n- object\n", encoding="utf-8")

    try:
        runtime_config.load_runtime_config(selected)
    except ValueError as exc:
        assert "YAML对象" in str(exc)
    else:
        raise AssertionError("non-mapping yaml must fail")


def test_missing_runtime_file_does_not_fall_back_to_example(tmp_path) -> None:
    missing = tmp_path / "runtime.local.yaml"

    try:
        runtime_config.load_runtime_config(missing)
    except FileNotFoundError as exc:
        assert str(missing) in str(exc)
    else:
        raise AssertionError("missing runtime.local.yaml must fail")
