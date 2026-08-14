from __future__ import annotations

import requests

from factor_service.research.control import ResearchControlError
from factor_service.research.errors import PermanentJobError, classify_exception


def test_error_classification_is_explicit() -> None:
    assert classify_exception(requests.ConnectionError("offline"))[0] is True
    assert classify_exception(ResearchControlError(
        "offline", retryable=True, code="control_database_transient",
    ))[0] is True
    assert classify_exception(ResearchControlError(
        "bad job", retryable=False, code="model_research_rejected",
    ))[0] is False
    assert classify_exception(PermanentJobError("bad data"))[0] is False
    assert classify_exception(RuntimeError("unknown"))[0] is False
