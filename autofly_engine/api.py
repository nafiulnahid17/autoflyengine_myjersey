from __future__ import annotations

import hmac
import json
import re
from contextlib import asynccontextmanager
from typing import Callable

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask

from . import __version__
from .artboard import ArtboardClient, UploadPayload
from .config import Settings, Upstream
from .errors import AutoflyError
from .models import BriefJobRequest, ProcessingMode, ProcessingOptions
from .service import AutoflyService

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


async def _read_upload(file: UploadFile, maximum: int) -> UploadPayload:
    content = await file.read(maximum + 1)
    await file.close()
    if not content:
        raise AutoflyError("EMPTY_UPLOAD", "Uploaded input is empty.")
    if len(content) > maximum:
        raise AutoflyError("UPLOAD_TOO_LARGE", "Uploaded input exceeds the configured limit.", 413)
    filename = (file.filename or "input.bin").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return UploadPayload(filename=filename, content=content, content_type=file.content_type)


def create_app(
    settings: Settings,
    client_factory: Callable[[Upstream], ArtboardClient] = ArtboardClient,
    service: AutoflyService | None = None,
) -> FastAPI:
    engine = service or AutoflyService(settings, client_factory)

    @asynccontextmanager
    async def lifespan(_app):
        engine.resume()
        yield
        engine.close()

    app = FastAPI(
        title="My Jersey Autofly Engine",
        version=__version__,
        description="One-click orchestration for the unchanged Artboard AI engine.",
        docs_url="/api/autofly/docs",
        redoc_url=None,
        openapi_url="/api/autofly/openapi.json",
        lifespan=lifespan,
    )
    app.state.autofly_service = engine
    app.state.settings = settings

    @app.exception_handler(AutoflyError)
    async def autofly_error(_request, exc: AutoflyError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @app.exception_handler(ValidationError)
    async def validation_error(_request, _exc: ValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "INVALID_REQUEST", "message": "Request validation failed."}},
        )

    async def access(authorization: str | None = Header(default=None)):
        if settings.inbound_token:
            supplied = (authorization or "").removeprefix("Bearer ")
            if not hmac.compare_digest(supplied, settings.inbound_token):
                raise HTTPException(401, "Autofly engine token required.")

    @app.get("/api/healthz")
    def health():
        routes = {}
        for mode in ProcessingMode:
            upstream = settings.upstream_for(mode)
            routes[mode.value] = {
                "upstream": upstream.key,
                "dedicated": settings.route_is_dedicated(mode),
            }
        return {"status": "ok", "engine": "Autofly", "version": __version__, "routes": routes}

    @app.get("/api/autofly/capabilities", dependencies=[Depends(access)])
    def capabilities():
        return {
            "engine": "Autofly",
            "version": __version__,
            "processing": "server-to-server Artboard AI orchestration",
            "artboard_repository_modified": False,
            "workflows": {
                "brief": "Generate front/back artwork from a written brief.",
                "panels": "Process separately supplied front and back panels.",
                "combined": "Detect and process one image that visibly contains front and back panels.",
            },
            "modes": {
                mode.value: {
                    "dedicated_upstream": settings.route_is_dedicated(mode),
                    "note": "A dedicated Artboard deployment is required for a distinct model tier."
                    if not settings.route_is_dedicated(mode)
                    else "Mode routes to its dedicated Artboard deployment.",
                }
                for mode in ProcessingMode
            },
            "limits": {"upload_mib": settings.max_upload_bytes // (1024 * 1024)},
            "safety": {
                "manual_handoff_on_uncertain_detection": True,
                "ambiguous_job_creation_not_auto_retried": True,
                "secrets_exposed_to_browser": False,
            },
        }

    @app.post("/api/autofly/jobs/from-brief", status_code=202, dependencies=[Depends(access)])
    def from_brief(payload: BriefJobRequest):
        return engine.submit_brief(payload).public()

    @app.post("/api/autofly/jobs/from-panels", status_code=202, dependencies=[Depends(access)])
    async def from_panels(
        front: UploadFile = File(...),
        back: UploadFile = File(...),
        options_json: str = Form(default="{}"),
    ):
        try:
            options = ProcessingOptions.model_validate_json(options_json)
        except ValidationError as exc:
            raise AutoflyError("INVALID_OPTIONS", "Processing options JSON is invalid.") from exc
        front_payload = await _read_upload(front, settings.max_upload_bytes)
        back_payload = await _read_upload(back, settings.max_upload_bytes)
        return engine.submit_panels(front_payload, back_payload, options).public()

    @app.post("/api/autofly/jobs/from-combined", status_code=202, dependencies=[Depends(access)])
    async def from_combined(
        file: UploadFile = File(...),
        options_json: str = Form(default="{}"),
    ):
        try:
            options = ProcessingOptions.model_validate_json(options_json)
        except ValidationError as exc:
            raise AutoflyError("INVALID_OPTIONS", "Processing options JSON is invalid.") from exc
        source = await _read_upload(file, settings.max_upload_bytes)
        return engine.submit_combined(source, options).public()

    @app.get("/api/autofly/jobs/{job_id}", dependencies=[Depends(access)])
    def get_job(job_id: str):
        return engine.get(job_id).public()

    @app.post("/api/autofly/jobs/{job_id}/cancel", dependencies=[Depends(access)])
    def cancel(job_id: str):
        return engine.cancel(job_id).public()

    async def remote_stream(job_id: str, remote_path: str, download_name: str | None = None):
        state = engine.get(job_id)
        if state.status not in {"ready", "awaiting_desktop"}:
            raise AutoflyError("OUTPUT_NOT_READY", "Autofly outputs are not ready.", 409)
        upstream = settings.upstream_for(state.mode)
        client = httpx.AsyncClient(
            base_url=upstream.url,
            headers={"Authorization": f"Bearer {upstream.token}"},
            timeout=httpx.Timeout(connect=15, read=300, write=30, pool=15),
            follow_redirects=False,
        )
        request = client.build_request("GET", remote_path)
        try:
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise AutoflyError("DOWNLOAD_UNAVAILABLE", "Artboard output is temporarily unavailable.", 503) from exc
        if not response.is_success:
            data = await response.aread()
            await response.aclose()
            await client.aclose()
            message = "Artboard output download failed."
            try:
                payload = json.loads(data)
                message = str(payload.get("detail", payload.get("error", message)))
            except Exception:
                pass
            raise AutoflyError("DOWNLOAD_FAILED", message, response.status_code)

        async def close_remote():
            await response.aclose()
            await client.aclose()

        headers = {"Cache-Control": "no-store"}
        if download_name:
            headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
        return StreamingResponse(
            response.aiter_bytes(),
            media_type=response.headers.get("content-type", "application/octet-stream"),
            headers=headers,
            background=BackgroundTask(close_remote),
        )

    @app.get("/api/autofly/jobs/{job_id}/bundle", dependencies=[Depends(access)])
    async def bundle(job_id: str):
        state = engine.get(job_id)
        return await remote_stream(
            job_id,
            f"/api/engine/jobs/{state.remote_job_id}/bundle",
            f"autofly-{job_id}.zip",
        )

    @app.get("/api/autofly/jobs/{job_id}/files/{filename}", dependencies=[Depends(access)])
    async def file(job_id: str, filename: str):
        if not SAFE_FILENAME.fullmatch(filename):
            raise AutoflyError("INVALID_FILENAME", "Invalid output filename.", 404)
        state = engine.get(job_id)
        return await remote_stream(
            job_id,
            f"/api/engine/jobs/{state.remote_job_id}/files/{filename}",
            filename,
        )

    return app

