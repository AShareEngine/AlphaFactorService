from __future__ import annotations

import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from typing import Any
from urllib.parse import urlsplit

from factor_service.research import __version__
from factor_service.research.config import Settings
from factor_service.research.errors import PermanentJobError


class WorkerHttpService:
    def __init__(self, worker: Any, settings: Settings) -> None:
        self.worker = worker
        self.settings = settings
        self.server = ThreadingHTTPServer(
            (settings.service_host, settings.service_port),
            _handler(worker, settings),
        )
        self.server.daemon_threads = True

    def serve_forever(self) -> None:
        self.server.timeout = 0.5
        while not self.worker.stopping:
            self.server.handle_request()

    def close(self) -> None:
        self.server.server_close()


def _handler(worker: Any, settings: Settings) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"AlphaFactorResearchWorker/{__version__}"

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._send(HTTPStatus.OK, {"ok": True, "service": "AlphaFactorResearchWorker"})
                return
            if path == "/ready":
                status = worker.status()
                code = HTTPStatus.OK if status["ready"] else HTTPStatus.SERVICE_UNAVAILABLE
                self._send(code, {
                    "ok": bool(status["ready"]),
                    "service": "AlphaFactorResearchWorker",
                    "ready": status["ready"],
                    "busy": status["busy"],
                })
                return
            if path == "/api/v1/status":
                if not self._authorized():
                    return
                self._send(HTTPStatus.OK, worker.status())
                return
            self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": "接口不存在"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path != "/api/v1/jobs":
                self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": "接口不存在"})
                return
            if not self._authorized():
                return
            try:
                payload = self._json_body()
                result = worker.submit(payload)
            except PermanentJobError as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            except RuntimeError as exc:
                self._send(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                return
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                return
            self._send(HTTPStatus.ACCEPTED, {"ok": True, "job": result})

        def _authorized(self) -> bool:
            token = settings.worker_token.strip()
            if not token:
                return True
            supplied = str(self.headers.get("Authorization") or "")
            if hmac.compare_digest(supplied, f"Bearer {token}"):
                return True
            self._send(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Worker服务认证失败"})
            return False

        def _json_body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise ValueError("Content-Length无效") from exc
            if length <= 0 or length > 10 * 1024 * 1024:
                raise ValueError("请求体不能为空且不得超过10MiB")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是JSON对象")
            return payload

        def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, pattern: str, *args: object) -> None:
            print(f"Worker HTTP {self.address_string()} - {pattern % args}", flush=True)

    return Handler


__all__ = ["WorkerHttpService"]
