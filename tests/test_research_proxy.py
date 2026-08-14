from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.error import URLError

from factor_service.api import research


class _Response:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


def test_research_gateway_forwards_job_and_authorization(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        research, "settings",
        lambda: SimpleNamespace(research_internal_url="http://127.0.0.1:8787"),
    )

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response(202, {"ok": True, "job": {"job_id": "job-1"}})

    monkeypatch.setattr(research, "urlopen", fake_urlopen)

    response = research.research_job_submit(
        {"job_id": "job-1"}, authorization="Bearer internal-token",
    )

    assert response.status_code == 202
    assert captured == {
        "url": "http://127.0.0.1:8787/api/v1/jobs",
        "method": "POST",
        "authorization": "Bearer internal-token",
        "payload": {"job_id": "job-1"},
        "timeout": 15,
    }


def test_research_gateway_fails_closed_when_process_is_offline(monkeypatch) -> None:
    monkeypatch.setattr(
        research, "settings",
        lambda: SimpleNamespace(research_internal_url="http://127.0.0.1:8787"),
    )
    monkeypatch.setattr(
        research, "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )

    response = research.research_ready()

    assert response.status_code == 503
    assert b"research" not in response.body.lower()
    assert "研究调度进程不可用" in response.body.decode("utf-8")


def test_research_gateway_rejects_non_loopback_upstream(monkeypatch) -> None:
    monkeypatch.setattr(
        research, "settings",
        lambda: SimpleNamespace(research_internal_url="http://10.126.126.4:8787"),
    )

    try:
        research._internal_base_url()
    except RuntimeError as exc:
        assert "本机HTTP地址" in str(exc)
    else:
        raise AssertionError("非本机研究进程地址不应被接受")
