from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Upstream
from .errors import AutoflyError


@dataclass(frozen=True)
class UploadPayload:
    filename: str
    content: bytes
    content_type: str | None = None


class ArtboardClient:
    """Private server-to-server client for the unchanged Artboard AI API."""

    def __init__(self, upstream: Upstream):
        self.upstream = upstream
        self.headers = {"Authorization": f"Bearer {upstream.token}"}
        self.timeout = httpx.Timeout(connect=15, read=180, write=180, pool=15)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.upstream.url,
            headers=self.headers,
            timeout=self.timeout,
            follow_redirects=False,
        )

    @staticmethod
    def _raise(response: httpx.Response, operation: str) -> None:
        if response.is_success:
            return
        code = "ARTBOARD_UPSTREAM_ERROR"
        message = f"Artboard AI rejected {operation}."
        try:
            payload = response.json()
            detail = payload.get("error", payload.get("detail", payload))
            if isinstance(detail, dict):
                code = str(detail.get("code", code))
                message = str(detail.get("message", detail.get("detail", message)))
            elif isinstance(detail, str):
                message = detail
        except Exception:
            pass
        status = 503 if response.status_code >= 500 else response.status_code
        raise AutoflyError(code, message, status)

    def health(self) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.get("/api/healthz")
            self._raise(response, "health check")
            return response.json()
        except httpx.HTTPError as exc:
            raise AutoflyError("ARTBOARD_UNREACHABLE", "Artboard AI is unreachable.", 503) from exc

    def capabilities(self) -> dict[str, Any]:
        with self._client() as client:
            response = client.get("/api/engine/capabilities")
        self._raise(response, "capability check")
        return response.json()

    def upload(self, payload: UploadPayload) -> dict[str, Any]:
        content_type = payload.content_type or mimetypes.guess_type(payload.filename)[0] or "application/octet-stream"
        try:
            with self._client() as client:
                response = client.post(
                    "/api/engine/sources",
                    files={"file": (payload.filename, payload.content, content_type)},
                )
        except httpx.HTTPError as exc:
            raise AutoflyError("ARTBOARD_UPLOAD_FAILED", "Could not upload input to Artboard AI.", 503) from exc
        self._raise(response, "source upload")
        return response.json()

    def detect_panels(self, source_id: str, use_ai: bool = True) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                f"/api/engine/sources/{source_id}/detect-panels",
                params={"ai": str(use_ai).lower()},
            )
        self._raise(response, "panel detection")
        return response.json()

    def analyze_source(self, source_id: str, use_ai: bool = True) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                f"/api/engine/sources/{source_id}/analyze",
                params={"ai": str(use_ai).lower()},
            )
        self._raise(response, "source analysis")
        return response.json()

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.post("/api/engine/jobs", json=payload)
        except httpx.HTTPError as exc:
            raise AutoflyError(
                "ARTBOARD_JOB_SUBMISSION_UNCERTAIN",
                "Artboard job submission did not return a confirmed response; do not submit automatically again.",
                503,
            ) from exc
        self._raise(response, "job creation")
        return response.json()

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.post("/api/engine/generate", json=payload)
        except httpx.HTTPError as exc:
            raise AutoflyError(
                "ARTBOARD_JOB_SUBMISSION_UNCERTAIN",
                "Artwork generation submission did not return a confirmed response; do not submit automatically again.",
                503,
            ) from exc
        self._raise(response, "artwork generation")
        return response.json()

    def get_job(self, remote_job_id: str) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.get(f"/api/engine/jobs/{remote_job_id}")
        except httpx.HTTPError as exc:
            raise AutoflyError("ARTBOARD_STATUS_UNAVAILABLE", "Artboard job status is temporarily unavailable.", 503) from exc
        self._raise(response, "job status")
        return response.json()

    def cancel(self, remote_job_id: str) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(f"/api/engine/jobs/{remote_job_id}/cancel")
        self._raise(response, "job cancellation")
        return response.json()

