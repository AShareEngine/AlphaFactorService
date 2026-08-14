from __future__ import annotations

import json
import sys

from factor_service.model_artifacts import ModelArtifactStore
from factor_service.model_research_repository import ModelResearchRepository
from factor_service.research.control import ResearchControl
from factor_service.research.config import load_settings
from factor_service.research.dataset import DatasetBuilder


def doctor() -> None:
    settings = load_settings()
    settings.work_root.mkdir(parents=True, exist_ok=True)
    settings.model_artifacts_root.mkdir(parents=True, exist_ok=True)
    print("检查模型研究控制库", flush=True)
    print(
        f"检查 ClickHouse: {settings.clickhouse_host}:{settings.clickhouse_port}",
        flush=True,
    )
    print(f"检查研究文件目录: {settings.work_root}", flush=True)
    print(f"检查正式模型目录: {settings.model_artifacts_root}", flush=True)
    try:
        check = DatasetBuilder(settings).check()
        control = ResearchControl(
            ModelResearchRepository(), ModelArtifactStore(settings.model_artifacts_root),
        )
        control_check = control.check()
    except Exception as exc:
        print(f"诊断失败: {exc}", file=sys.stderr)
        print(
            "请检查config/runtime.local.yaml中的control_database和clickhouse配置。",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    print(json.dumps(
        {"ok": True, "clickhouse": check, "control_database": control_check},
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    doctor()
