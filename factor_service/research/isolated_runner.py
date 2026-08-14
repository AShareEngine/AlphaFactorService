from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from factor_service.model_artifacts import ModelArtifactStore
from factor_service.model_research_repository import ModelResearchRepository
from factor_service.research.control import ResearchControl
from factor_service.research.config import load_settings
from factor_service.research.inference import DailyInferenceRunner
from factor_service.research.trainer import QlibTrainer, TrainingResult


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one model task in an isolated process")
    parser.add_argument("kind", choices=("train", "infer"))
    parser.add_argument("job_path", type=Path)
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("result_path", type=Path)
    args = parser.parse_args()
    job = json.loads(args.job_path.read_text(encoding="utf-8"))
    settings = load_settings()
    if args.kind == "infer":
        result = DailyInferenceRunner(
            settings,
            ResearchControl(
                ModelResearchRepository(),
                ModelArtifactStore(settings.model_artifacts_root),
            ),
        ).run(job, args.work_dir)
    else:
        result = QlibTrainer(settings).train(job, args.work_dir)
    _write_result(args.result_path, result)


def _write_result(path: Path, result: TrainingResult) -> None:
    payload = {
        "result": result.result,
        "artifacts": [[kind, str(artifact)] for kind, artifact in result.artifacts],
        "predictions_path": str(result.predictions_path),
    }
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )
    os.replace(temporary, path)


if __name__ == "__main__":
    main()
