import tempfile
import time
from pathlib import Path

import pytest

from autofly_engine.artboard import UploadPayload
from autofly_engine.config import Settings, Upstream
from autofly_engine.errors import AutoflyError
from autofly_engine.models import BriefJobRequest, ProcessingMode, ProcessingOptions
from autofly_engine.service import AutoflyService


class FakeArtboardClient:
    instances = []

    def __init__(self, upstream):
        self.upstream = upstream
        self.status_calls = 0
        self.created_payload = None
        FakeArtboardClient.instances.append(self)

    def generate(self, payload):
        self.created_payload = payload
        return {"job_id": "remote-brief", "status": "queued", "progress": 0}

    def upload(self, payload):
        return {"source_id": "source-" + payload.filename}

    def create_job(self, payload):
        self.created_payload = payload
        return {"job_id": "remote-panels", "status": "queued", "progress": 0}

    def detect_panels(self, _source_id, use_ai=True):
        assert use_ai is True
        return {
            "ai_analysis": {
                "input_type": "mockup",
                "requires_manual_reconstruction": True,
            },
            "candidates": [
                {"suggested_side": "front", "source_bbox_px": {"x": 0, "y": 0, "width": 100, "height": 200}},
                {"suggested_side": "back", "source_bbox_px": {"x": 110, "y": 0, "width": 100, "height": 200}},
            ]
        }

    def analyze_source(self, _source_id, use_ai=True):
        assert use_ai is True
        return {
            "ai": {
                "input_type": "mockup",
                "requires_manual_reconstruction": True,
            }
        }

    def get_job(self, remote_job_id):
        self.status_calls += 1
        if self.status_calls == 1:
            return {"job_id": remote_job_id, "status": "processing", "stage": "trace", "progress": 55}
        return {
            "job_id": remote_job_id,
            "status": "review_required",
            "stage": "review_required",
            "progress": 100,
            "result": {"preflight": {"true_vector_paths": True}},
        }

    def cancel(self, remote_job_id):
        return {"job_id": remote_job_id, "status": "cancelled"}


def make_settings(root: Path) -> Settings:
    return Settings(
        workspace=root,
        inbound_token="test-token",
        default_upstream=Upstream("default", "https://artboard.example", "secret"),
        quality_upstream=Upstream("quality", "https://quality.example", "quality-secret"),
        cost_upstream=Upstream("cost_effective", "https://cost.example", "cost-secret"),
        max_upload_bytes=30 * 1024 * 1024,
        max_outstanding_jobs=4,
        poll_interval_seconds=0.01,
        job_timeout_seconds=60,
    )


def wait_ready(service, job_id):
    for _ in range(100):
        state = service.get(job_id)
        if state.status == "ready":
            return state
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_brief_routes_quality_and_finishes_ready():
    FakeArtboardClient.instances.clear()
    with tempfile.TemporaryDirectory() as temp:
        service = AutoflyService(make_settings(Path(temp)), FakeArtboardClient)
        state = service.submit_brief(BriefJobRequest(brief="Create a blue geometric jersey artwork"))
        completed = wait_ready(service, state.job_id)
        assert completed.result["preflight"]["true_vector_paths"] is True
        assert FakeArtboardClient.instances[0].upstream.key == "quality"
        service.close()


def test_panel_job_routes_cost_mode_and_preserves_artboard_contract():
    FakeArtboardClient.instances.clear()
    with tempfile.TemporaryDirectory() as temp:
        service = AutoflyService(make_settings(Path(temp)), FakeArtboardClient)
        options = ProcessingOptions(mode=ProcessingMode.COST_EFFECTIVE)
        state = service.submit_panels(
            UploadPayload("front.png", b"front", "image/png"),
            UploadPayload("back.png", b"back", "image/png"),
            options,
        )
        wait_ready(service, state.job_id)
        submission = next(x.created_payload for x in FakeArtboardClient.instances if x.created_payload)
        assert {panel["side"] for panel in submission["panels"]} == {"front", "back"}
        assert all(panel["reconstruction_mode"] == "ai_flat_artwork" for panel in submission["panels"])
        assert FakeArtboardClient.instances[0].upstream.key == "cost_effective"
        service.close()


def test_auto_analysis_traces_flat_artwork_without_regeneration():
    class FlatArtworkClient(FakeArtboardClient):
        def analyze_source(self, _source_id, use_ai=True):
            return {
                "ai": {
                    "input_type": "flat_artwork",
                    "requires_manual_reconstruction": False,
                }
            }

    FlatArtworkClient.instances = FakeArtboardClient.instances
    FakeArtboardClient.instances.clear()
    with tempfile.TemporaryDirectory() as temp:
        service = AutoflyService(make_settings(Path(temp)), FlatArtworkClient)
        state = service.submit_panels(
            UploadPayload("front.png", b"front", "image/png"),
            UploadPayload("back.png", b"back", "image/png"),
            ProcessingOptions(),
        )
        wait_ready(service, state.job_id)
        submission = next(x.created_payload for x in FakeArtboardClient.instances if x.created_payload)
        assert all(panel["reconstruction_mode"] == "trace_source" for panel in submission["panels"])
        service.close()


def test_combined_requires_unambiguous_front_and_back(monkeypatch):
    class UncertainClient(FakeArtboardClient):
        def detect_panels(self, _source_id, use_ai=True):
            return {
                "ai_analysis": {"input_type": "mockup", "requires_manual_reconstruction": True},
                "candidates": [{"suggested_side": "front", "source_bbox_px": {"x": 0, "y": 0, "width": 1, "height": 1}}],
            }

    with tempfile.TemporaryDirectory() as temp:
        service = AutoflyService(make_settings(Path(temp)), UncertainClient)
        with pytest.raises(AutoflyError) as raised:
            service.submit_combined(
                UploadPayload("combined.png", b"image", "image/png"),
                ProcessingOptions(),
            )
        assert raised.value.code == "AUTO_DETECTION_UNCERTAIN"
        service.close()

