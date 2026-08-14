from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


class JobStateStore:
    def __init__(self, work_root: Path) -> None:
        self.root = (Path(work_root) / "state").resolve()
        self.path = self.root / "active-job.json"

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None

    def save(self, job: dict[str, Any], phase: str, progress: dict[str, Any] | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "alphablocks.worker-state.v1",
            "job": job,
            "phase": str(phase),
            "progress": dict(progress or {}),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump(payload, target, ensure_ascii=False, sort_keys=True)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


__all__ = ["JobStateStore"]
