from __future__ import annotations

from pathlib import Path

import pandas as pd

from factor_service.research.config import Settings
from factor_service.research.trainer import QlibTrainer
from tests.research.utils import valid_inference_job, valid_job


class _ClickHouse:
    def __init__(self) -> None:
        self.commands = []
        self.inserts = []

    def command(self, query, parameters=None):
        self.commands.append((query, parameters))

    def insert(self, table, rows, column_names):
        self.inserts.append((table, rows, column_names))

    def query(self, _query, parameters=None):
        row_count = len(self.inserts[-1][1])
        return type("Result", (), {"result_rows": [[row_count]]})()


def test_prediction_publish_replaces_exact_inference_run(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(
        api_url="http://127.0.0.1/api/model-research", worker_token="",
        clickhouse_host="localhost", clickhouse_port=8123,
        clickhouse_user="default", clickhouse_password="",
        factor_database="ab_factor", model_database="ab_model",
        source_database="starlight", work_root=tmp_path,
        service_host="127.0.0.1", service_port=8787,
    )
    # Avoid DatasetBuilder's constructor connection; publish owns its own client.
    trainer = QlibTrainer.__new__(QlibTrainer)
    trainer.settings = settings
    client = _ClickHouse()
    monkeypatch.setattr(
        "factor_service.research.trainer.clickhouse_connect.get_client",
        lambda **_kwargs: client,
    )
    path = tmp_path / "predictions.parquet"
    frame = pd.DataFrame([{
        "trade_date": pd.Timestamp("2024-01-02"),
        "entity_code": "000001.SZ",
        "raw_prediction": 0.2,
        "rank_value": 1,
        "percentile": 1.0,
        "score": 1.0,
        "feature_cutoff_at": pd.Timestamp("2024-01-02 15:00:00", tz="Asia/Shanghai"),
        "computed_at": pd.Timestamp("2025-01-01 10:00:00", tz="Asia/Shanghai"),
        "source_vintage": "test",
    }])
    frame.to_parquet(path, index=False)
    job = valid_job()

    assert trainer.publish_predictions(path, job) == 1
    assert trainer.publish_predictions(path, job) == 1

    assert len(client.commands) == 2
    assert len(client.inserts) == 2
    for _, parameters in client.commands:
        assert parameters["model_id"] == "test_model"
        assert parameters["model_version"] == 1
        assert "inference_run_id" not in parameters


def test_daily_inference_publish_replaces_only_target_date(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(
        api_url="http://127.0.0.1/api/model-research", worker_token="",
        clickhouse_host="localhost", clickhouse_port=8123,
        clickhouse_user="default", clickhouse_password="",
        factor_database="ab_factor", model_database="ab_model",
        source_database="starlight", work_root=tmp_path,
        service_host="127.0.0.1", service_port=8787,
    )
    trainer = QlibTrainer.__new__(QlibTrainer)
    trainer.settings = settings
    client = _ClickHouse()
    monkeypatch.setattr(
        "factor_service.research.trainer.clickhouse_connect.get_client",
        lambda **_kwargs: client,
    )
    path = tmp_path / "daily.parquet"
    pd.DataFrame([{
        "trade_date": pd.Timestamp("2024-12-31"), "entity_code": "000001.SZ",
        "raw_prediction": 0.2, "rank_value": 1, "percentile": 1.0, "score": 1.0,
        "feature_cutoff_at": pd.Timestamp("2024-12-31 15:00:00", tz="Asia/Shanghai"),
        "computed_at": pd.Timestamp("2024-12-31 16:00:00", tz="Asia/Shanghai"),
        "source_vintage": "daily-test",
    }]).to_parquet(path, index=False)

    assert trainer.publish_predictions(path, valid_inference_job()) == 1

    query, parameters = client.commands[0]
    assert "trade_date IN" in query
    assert parameters["trade_dates"] == [pd.Timestamp("2024-12-31").date()]
