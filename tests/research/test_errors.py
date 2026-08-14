from __future__ import annotations

import requests

from factor_service.research.api import AlphaBlocksApiError
from factor_service.research.errors import PermanentJobError, classify_exception


def test_error_classification_is_explicit() -> None:
    assert classify_exception(requests.ConnectionError("offline"))[0] is True
    assert classify_exception(AlphaBlocksApiError("busy", status_code=503))[0] is True
    assert classify_exception(AlphaBlocksApiError("bad job", status_code=400))[0] is False
    assert classify_exception(PermanentJobError("bad data"))[0] is False
    assert classify_exception(RuntimeError("unknown"))[0] is False
