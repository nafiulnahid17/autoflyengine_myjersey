from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .errors import AutoflyError
from .models import ProcessingMode


def _positive_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise AutoflyError("INVALID_CONFIGURATION", f"{name} must be an integer.", 500) from exc
    if not minimum <= value <= maximum:
        raise AutoflyError(
            "INVALID_CONFIGURATION",
            f"{name} must be between {minimum} and {maximum}.",
            500,
        )
    return value


def _positive_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise AutoflyError("INVALID_CONFIGURATION", f"{name} must be numeric.", 500) from exc
    if not minimum <= value <= maximum:
        raise AutoflyError(
            "INVALID_CONFIGURATION",
            f"{name} must be between {minimum} and {maximum}.",
            500,
        )
    return value


def _clean_url(value: str, name: str, required: bool = False) -> str | None:
    value = value.strip().rstrip("/")
    if not value:
        if required:
            raise AutoflyError("INVALID_CONFIGURATION", f"{name} is required.", 500)
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AutoflyError("INVALID_CONFIGURATION", f"{name} must be an HTTP(S) URL.", 500)
    return value


@dataclass(frozen=True)
class Upstream:
    key: str
    url: str
    token: str


@dataclass(frozen=True)
class Settings:
    workspace: Path
    inbound_token: str | None
    default_upstream: Upstream
    quality_upstream: Upstream | None
    cost_upstream: Upstream | None
    max_upload_bytes: int
    max_outstanding_jobs: int
    poll_interval_seconds: float
    job_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        base_url = _clean_url(
            os.environ.get("ARTBOARD_ENGINE_URL", ""),
            "ARTBOARD_ENGINE_URL",
            required=True,
        )
        base_token = os.environ.get("ARTBOARD_ENGINE_TOKEN", "").strip()
        if not base_token:
            raise AutoflyError("INVALID_CONFIGURATION", "ARTBOARD_ENGINE_TOKEN is required.", 500)

        def optional_upstream(prefix: str, key: str) -> Upstream | None:
            url = _clean_url(os.environ.get(f"ARTBOARD_{prefix}_URL", ""), f"ARTBOARD_{prefix}_URL")
            if not url:
                return None
            token = os.environ.get(f"ARTBOARD_{prefix}_TOKEN", "").strip() or base_token
            return Upstream(key=key, url=url, token=token)

        upload_mib = _positive_int("AUTOFLY_MAX_UPLOAD_MIB", 30, 1, 100)
        return cls(
            workspace=Path(os.environ.get("AUTOFLY_WORKSPACE", ".runtime")).resolve(),
            inbound_token=os.environ.get("AUTOFLY_ENGINE_TOKEN", "").strip() or None,
            default_upstream=Upstream("default", base_url, base_token),
            quality_upstream=optional_upstream("QUALITY", "quality"),
            cost_upstream=optional_upstream("COST", "cost_effective"),
            max_upload_bytes=upload_mib * 1024 * 1024,
            max_outstanding_jobs=_positive_int("AUTOFLY_MAX_OUTSTANDING_JOBS", 24, 1, 200),
            poll_interval_seconds=_positive_float("AUTOFLY_POLL_INTERVAL_SECONDS", 2, 0.1, 60),
            job_timeout_seconds=_positive_int("AUTOFLY_JOB_TIMEOUT_SECONDS", 1800, 60, 21600),
        )

    def upstream_for(self, mode: ProcessingMode) -> Upstream:
        if mode is ProcessingMode.QUALITY and self.quality_upstream:
            return self.quality_upstream
        if mode is ProcessingMode.COST_EFFECTIVE and self.cost_upstream:
            return self.cost_upstream
        return self.default_upstream

    def route_is_dedicated(self, mode: ProcessingMode) -> bool:
        return self.upstream_for(mode).key != "default"

