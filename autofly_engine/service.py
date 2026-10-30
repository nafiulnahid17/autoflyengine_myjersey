from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .artboard import ArtboardClient, UploadPayload
from .config import Settings, Upstream
from .errors import AutoflyError
from .models import BriefJobRequest, JobState, ProcessingMode, ProcessingOptions, Workflow
from .store import JobStore

logger = logging.getLogger("autofly_engine")

TERMINAL_REMOTE = {"review_required", "awaiting_desktop", "approved", "failed", "cancelled"}


class AutoflyService:
    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[Upstream], ArtboardClient] = ArtboardClient,
    ):
        self.settings = settings
        self.store = JobStore(settings.workspace)
        self.client_factory = client_factory
        self.executor = ThreadPoolExecutor(
            max_workers=settings.max_outstanding_jobs,
            thread_name_prefix="autofly-monitor",
        )
        self.lock = threading.RLock()
        self.futures: dict[str, object] = {}

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def resume(self) -> None:
        for state in self.store.active():
            self._start_monitor(state.job_id, ProcessingMode(state.mode))

    def _client(self, mode: ProcessingMode) -> ArtboardClient:
        return self.client_factory(self.settings.upstream_for(mode))

    def _register(
        self,
        workflow: Workflow,
        mode: ProcessingMode,
        remote: dict,
    ) -> JobState:
        remote_job_id = remote.get("job_id")
        if not isinstance(remote_job_id, str) or not remote_job_id:
            raise AutoflyError("ARTBOARD_INVALID_RESPONSE", "Artboard AI returned no job ID.", 502)
        job_id = str(uuid.uuid4())
        state = JobState(
            job_id=job_id,
            workflow=workflow,
            mode=mode,
            status="queued",
            stage=str(remote.get("stage", "queued")),
            progress=int(remote.get("progress", 0)),
            remote_job_id=remote_job_id,
            upstream_key=self.settings.upstream_for(mode).key,
        )
        self.store.create(state)
        self._start_monitor(job_id, mode)
        return state

    def _start_monitor(self, job_id: str, mode: ProcessingMode) -> None:
        with self.lock:
            existing = self.futures.get(job_id)
            if existing and not getattr(existing, "done", lambda: True)():
                return
            future = self.executor.submit(self._monitor, job_id, mode)
            self.futures[job_id] = future

            def completed(_future):
                with self.lock:
                    self.futures.pop(job_id, None)

            future.add_done_callback(completed)

    def _monitor(self, job_id: str, mode: ProcessingMode) -> None:
        client = self._client(mode)
        deadline = time.monotonic() + self.settings.job_timeout_seconds
        transient_failures = 0
        while time.monotonic() < deadline:
            state = self.store.get(job_id)
            if state.status in {"ready", "awaiting_desktop", "failed", "cancelled"}:
                return
            try:
                remote = client.get_job(state.remote_job_id)
                transient_failures = 0
            except AutoflyError as exc:
                transient_failures += 1
                if transient_failures >= 5:
                    self.store.update(
                        job_id,
                        status="failed",
                        stage="status_unavailable",
                        error={"code": exc.code, "message": str(exc)},
                    )
                    return
                time.sleep(self.settings.poll_interval_seconds)
                continue

            remote_status = str(remote.get("status", "processing"))
            progress = max(0, min(100, int(remote.get("progress", state.progress))))
            stage = str(remote.get("stage", remote_status))
            if remote_status in TERMINAL_REMOTE:
                if remote_status == "failed":
                    self.store.update(
                        job_id,
                        status="failed",
                        stage=stage,
                        progress=progress,
                        result=remote.get("result"),
                        error=remote.get("error") or {
                            "code": "ARTBOARD_JOB_FAILED",
                            "message": "Artboard AI could not complete the workflow.",
                        },
                    )
                elif remote_status == "cancelled":
                    self.store.update(job_id, status="cancelled", stage=stage, progress=progress)
                else:
                    self.store.update(
                        job_id,
                        status="awaiting_desktop" if remote_status == "awaiting_desktop" else "ready",
                        stage=stage,
                        progress=100,
                        result=remote.get("result", remote),
                    )
                return

            self.store.update(
                job_id,
                status="processing" if remote_status == "processing" else "queued",
                stage=stage,
                progress=progress,
            )
            time.sleep(self.settings.poll_interval_seconds)

        self.store.update(
            job_id,
            status="failed",
            stage="monitor_timeout",
            error={
                "code": "AUTOFLY_MONITOR_TIMEOUT",
                "message": "Autofly stopped monitoring after the configured timeout. Check the Artboard job before resubmitting.",
            },
        )

    def submit_brief(self, payload: BriefJobRequest) -> JobState:
        remote = self._client(payload.mode).generate({
            "brief": payload.brief,
            "front_notes": payload.front_notes,
            "back_notes": payload.back_notes,
            "dpi": payload.dpi,
            "formats": payload.formats,
        })
        return self._register(Workflow.BRIEF, payload.mode, remote)

    @staticmethod
    def _choose_reconstruction(requested: str, analysis: dict) -> str:
        if requested != "auto":
            return requested
        ai = analysis.get("ai", analysis.get("ai_analysis", {}))
        input_type = ai.get("input_type")
        needs_reconstruction = bool(ai.get("requires_manual_reconstruction"))
        if input_type == "flat_artwork" and not needs_reconstruction:
            return "trace_source"
        if input_type in {"mockup", "photograph"} or needs_reconstruction:
            return "ai_flat_artwork"
        raise AutoflyError(
            "AUTO_ANALYSIS_UNCERTAIN",
            "Autofly could not safely choose source tracing or flat-art reconstruction. Continue in Artboard AI for manual review.",
            409,
        )

    @staticmethod
    def _panel_payload(
        source_id: str,
        side: str,
        options: ProcessingOptions,
        reconstruction_mode: str,
        crop: dict | None = None,
    ) -> dict:
        payload = {
            "side": side,
            "source_id": source_id,
            "reconstruction_mode": reconstruction_mode,
            "remove_edge_background": options.remove_edge_background,
            "background_tolerance": options.background_tolerance,
            "run_ocr": options.run_ocr,
            "ocr_language": options.ocr_language,
            "trace": options.trace.model_dump(),
            "text_layers": [],
            "alpha_matte_rgb": options.alpha_matte_rgb,
            "reference_patches": [],
        }
        if crop:
            payload["crop"] = crop
        return payload

    def submit_panels(
        self,
        front: UploadPayload,
        back: UploadPayload,
        options: ProcessingOptions,
    ) -> JobState:
        client = self._client(options.mode)
        front_source = client.upload(front)
        back_source = client.upload(back)
        front_analysis = client.analyze_source(front_source["source_id"], use_ai=True)
        back_analysis = client.analyze_source(back_source["source_id"], use_ai=True)
        front_mode = self._choose_reconstruction(options.reconstruction_mode, front_analysis)
        back_mode = self._choose_reconstruction(options.reconstruction_mode, back_analysis)
        remote = client.create_job({
            "panels": [
                self._panel_payload(front_source["source_id"], "front", options, front_mode),
                self._panel_payload(back_source["source_id"], "back", options, back_mode),
            ],
            "dpi": options.dpi,
            "formats": options.formats,
            "native_ai_mode": options.native_ai_mode,
        })
        return self._register(Workflow.PANELS, options.mode, remote)

    def submit_combined(self, source: UploadPayload, options: ProcessingOptions) -> JobState:
        client = self._client(options.mode)
        uploaded = client.upload(source)
        detection = client.detect_panels(uploaded["source_id"], use_ai=True)
        candidates = detection.get("candidates", [])
        by_side: dict[str, dict] = {}
        for candidate in candidates:
            side = candidate.get("suggested_side")
            box = candidate.get("source_bbox_px")
            if side in {"front", "back"} and isinstance(box, dict):
                if side in by_side:
                    raise AutoflyError(
                        "AUTO_DETECTION_UNCERTAIN",
                        f"Multiple {side} candidates were detected. Continue in Artboard AI for manual confirmation.",
                        409,
                    )
                by_side[side] = box
        if set(by_side) != {"front", "back"}:
            raise AutoflyError(
                "AUTO_DETECTION_UNCERTAIN",
                "One-click mode could not safely identify exactly one front and one back panel. Upload separate panels or continue in Artboard AI.",
                409,
            )
        reconstruction_mode = self._choose_reconstruction(options.reconstruction_mode, detection)
        remote = client.create_job({
            "panels": [
                self._panel_payload(
                    uploaded["source_id"], "front", options, reconstruction_mode, by_side["front"],
                ),
                self._panel_payload(
                    uploaded["source_id"], "back", options, reconstruction_mode, by_side["back"],
                ),
            ],
            "dpi": options.dpi,
            "formats": options.formats,
            "native_ai_mode": options.native_ai_mode,
        })
        return self._register(Workflow.COMBINED, options.mode, remote)

    def get(self, job_id: str) -> JobState:
        return self.store.get(job_id)

    def cancel(self, job_id: str) -> JobState:
        state = self.store.get(job_id)
        if state.status not in {"queued", "processing"}:
            raise AutoflyError("JOB_NOT_CANCELLABLE", "This Autofly job can no longer be cancelled.", 409)
        client = self._client(state.mode)
        client.cancel(state.remote_job_id)
        return self.store.update(job_id, status="cancelled", stage="cancelled")

