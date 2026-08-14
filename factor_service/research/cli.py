from __future__ import annotations

import argparse
import json
import sys

from factor_service.research.api import AlphaBlocksApi
from factor_service.research.config import load_settings
from factor_service.research.dataset import DatasetBuilder
from factor_service.research.worker import ResearchWorker


def main() -> None:
    parser = argparse.ArgumentParser(prog="alpha-factor-research-worker")
    parser.add_argument("command", choices=["run", "serve", "doctor"], nargs="?", default="run")
    args = parser.parse_args()
    settings = load_settings()
    worker = ResearchWorker(settings)
    if args.command == "doctor":
        print(f"检查 AlphaBlocks API: {settings.api_url}", flush=True)
        print(
            f"检查 ClickHouse: {settings.clickhouse_host}:{settings.clickhouse_port}",
            flush=True,
        )
        print(f"检查研究文件目录: {settings.work_root}", flush=True)
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
        return
    worker.run()


if __name__ == "__main__":
    main()
