from __future__ import annotations

import json
import sys

from factor_service.model_diagnostics import artifact_model_permutation_importance


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = artifact_model_permutation_importance(
            payload["bundle_path"],
            payload["dataset_path"],
            model_kind=payload["model_kind"],
            segments=payload["segments"],
            model_params=payload["model_params"],
            feature_names=payload["feature_names"],
        )
        json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
