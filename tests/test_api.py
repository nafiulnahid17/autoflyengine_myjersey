import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from autofly_engine.api import create_app
from autofly_engine.config import Settings, Upstream


class StubService:
    def resume(self):
        pass

    def close(self):
        pass


def settings(root: Path) -> Settings:
    return Settings(
        workspace=root,
        inbound_token="inbound-secret",
        default_upstream=Upstream("default", "https://artboard.example", "outbound-secret"),
        quality_upstream=None,
        cost_upstream=None,
        max_upload_bytes=30 * 1024 * 1024,
        max_outstanding_jobs=4,
        poll_interval_seconds=1,
        job_timeout_seconds=60,
    )


def test_private_capabilities_and_truthful_mode_reporting():
    with tempfile.TemporaryDirectory() as temp:
        app = create_app(settings(Path(temp)), service=StubService())
        with TestClient(app) as client:
            assert client.get("/api/autofly/capabilities").status_code == 401
            response = client.get(
                "/api/autofly/capabilities",
                headers={"Authorization": "Bearer inbound-secret"},
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["artboard_repository_modified"] is False
            assert payload["modes"]["quality"]["dedicated_upstream"] is False
            assert payload["modes"]["cost_effective"]["dedicated_upstream"] is False


def test_health_does_not_expose_secrets_or_upstream_urls():
    with tempfile.TemporaryDirectory() as temp:
        app = create_app(settings(Path(temp)), service=StubService())
        with TestClient(app) as client:
            response = client.get("/api/healthz")
            assert response.status_code == 200
            body = response.text
            assert "outbound-secret" not in body
            assert "artboard.example" not in body

