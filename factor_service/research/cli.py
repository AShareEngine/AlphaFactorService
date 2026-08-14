from __future__ import annotations

import json
import sys

from factor_service.research.api import AlphaBlocksApi
from factor_service.research.config import load_settings
from factor_service.research.dataset import DatasetBuilder


def doctor() -> None:
    settings = load_settings()
    settings.work_root.mkdir(parents=True, exist_ok=True)
    settings.model_artifacts_root.mkdir(parents=True, exist_ok=True)
    print(f"检查 AlphaBlocks API: {settings.api_url}", flush=True)
    print(
        f"检查 ClickHouse: {settings.clickhouse_host}:{settings.clickhouse_port}",
        flush=True,
    )
    print(f"检查研究文件目录: {settings.work_root}", flush=True)
    print(f"检查正式模型目录: {settings.model_artifacts_root}", flush=True)
    try:
        check = DatasetBuilder(settings).check()
        api = AlphaBlocksApi(settings.api_url, settings.worker_token)
        api_check = api.check()
    except Exception as exc:
        print(f"诊断失败: {exc}", file=sys.stderr)
        print(
            "请检查config/runtime.local.yaml中的research.api_url和clickhouse配置。",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    print(json.dumps({"ok": True, "clickhouse": check, "api": api_check}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    doctor()
