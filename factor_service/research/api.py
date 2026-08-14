from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

import requests

from factor_service.research.errors import JobError


class AlphaBlocksApiError(JobError):
    def __init__(self, message: str, *, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.retryable = status_code == 0 or status_code in {408, 425, 429} or status_code >= 500
        self.code = "alphablocks_api_transient" if self.retryable else "alphablocks_api_rejected"


class AlphaBlocksApi:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        # AlphaBlocks is a trusted LAN service. A macOS login session may
        # export proxy variables for desktop proxy tools; callbacks and large
        # artifact uploads must still connect directly to the LAN endpoint.
        self.session.trust_env = False
        if str(token or "").strip():
            self.session.headers.update({"Authorization": f"Bearer {token.strip()}"})

    def check(self) -> dict[str, Any]:
        response = self._json("GET", "/jobs?limit=1")
        return {"ok": bool(response.get("ok")), "reachable": True}

    def download_artifact(self, artifact_id: str, destination: Path, expected_sha256: str) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        digest = sha256()
        response = self.session.get(
            f"{self.base_url}/artifacts/{artifact_id}/download",
            stream=True,
            timeout=max(self.timeout, 600),
        )
        if not response.ok:
            self._response(response)
        try:
            with temporary.open("wb") as target:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        digest.update(chunk)
                        target.write(chunk)
            actual = digest.hexdigest()
            if actual != str(expected_sha256).lower():
                raise AlphaBlocksApiError("下载的模型产物SHA256不一致", status_code=422)
            temporary.replace(destination)
            return destination
        finally:
            response.close()
            if temporary.exists():
                temporary.unlink()

    def renew(self, job_id: str, lease_token: str, progress: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", f"/worker/jobs/{job_id}/heartbeat", {
            "lease_token": lease_token, "lease_seconds": 90, "progress": progress,
        })

    def control(self, job_id: str, lease_token: str) -> dict[str, Any]:
        response = self._json("POST", f"/worker/jobs/{job_id}/control", {
            "lease_token": lease_token,
        })
        return dict(response.get("control") or {})

    def stage(self, job_id: str, lease_token: str, stage: str, progress: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", f"/worker/jobs/{job_id}/stage", {
            "lease_token": lease_token, "stage": stage, "progress": progress,
        })

    def upload(
        self, job_id: str, lease_token: str, kind: str, path: Path,
        *, checkpoint=None, progress=None,
    ) -> dict[str, Any]:
        digest = _file_sha256(path)
        # Stable per job/kind/file so an interrupted retry overwrites its temporary
        # chunks instead of leaking a new .uploads directory on every attempt.
        upload_id = sha256(f"{job_id}:{kind}:{path.name}".encode()).hexdigest()[:32]
        chunk_size = 8 * 1024 * 1024
        total_chunks = max(1, (path.stat().st_size + chunk_size - 1) // chunk_size)
        with path.open("rb") as source:
            for chunk_index in range(total_chunks):
                if checkpoint is not None:
                    checkpoint()
                body = source.read(chunk_size)
                response = self.session.put(
                    f"{self.base_url}/worker/jobs/{job_id}/artifact-chunks/{kind}/{path.name}/{upload_id}/{chunk_index}",
                    headers={
                        "X-Lease-Token": lease_token,
                        "X-Chunk-SHA256": sha256(body).hexdigest(),
                        "Content-Type": "application/octet-stream",
                    },
                    data=body,
                    timeout=max(self.timeout, 120),
                )
                self._response(response)
                if progress is not None:
                    progress(chunk_index + 1, total_chunks)
        if checkpoint is not None:
            checkpoint()
        response = self.session.post(
            f"{self.base_url}/worker/jobs/{job_id}/artifact-chunks/{kind}/{path.name}/{upload_id}/complete",
            headers={"X-Lease-Token": lease_token},
            json={"total_chunks": total_chunks, "sha256": digest},
            timeout=max(self.timeout, 600),
        )
        return self._response(response)

    def complete(self, job_id: str, lease_token: str, result: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", f"/worker/jobs/{job_id}/complete", {
            "lease_token": lease_token, "result": result,
        })

    def fail(self, job_id: str, lease_token: str, error: str, retryable: bool = True) -> dict[str, Any]:
        return self._json("POST", f"/worker/jobs/{job_id}/fail", {
            "lease_token": lease_token, "error_message": error, "retryable": retryable,
        })

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.request(
            method, f"{self.base_url}{path}", json=payload, timeout=self.timeout,
        )
        return self._response(response)

    @staticmethod
    def _response(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": response.text[:500]}
        if not response.ok:
            raise AlphaBlocksApiError(
                str(payload.get("error") or payload),
                status_code=int(getattr(response, "status_code", 0) or 0),
            )
        return payload


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["AlphaBlocksApi", "AlphaBlocksApiError"]
