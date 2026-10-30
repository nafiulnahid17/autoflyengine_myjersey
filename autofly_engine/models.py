from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ProcessingMode(str, Enum):
    QUALITY = "quality"
    COST_EFFECTIVE = "cost_effective"


class Workflow(str, Enum):
    BRIEF = "brief"
    PANELS = "panels"
    COMBINED = "combined"


class Crop(StrictModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class TraceSettings(StrictModel):
    mode: Literal["spline", "polygon"] = "spline"
    color_precision: int = Field(default=6, ge=1, le=8)
    filter_speckle: int = Field(default=4, ge=0, le=64)
    corner_threshold: int = Field(default=60, ge=0, le=180)
    length_threshold: float = Field(default=4, ge=3, le=10)
    splice_threshold: int = Field(default=45, ge=0, le=180)
    max_dimension: int = Field(default=1800, ge=128, le=2400)


class ProcessingOptions(StrictModel):
    mode: ProcessingMode = ProcessingMode.QUALITY
    dpi: Literal[330, 600] = 600
    formats: list[Literal["svg", "eps", "ai"]] = Field(default_factory=lambda: ["svg", "eps"])
    native_ai_mode: Literal["disabled", "desktop_handoff"] = "disabled"
    reconstruction_mode: Literal["auto", "trace_source", "ai_flat_artwork"] = "auto"
    remove_edge_background: bool = False
    background_tolerance: int = Field(default=18, ge=0, le=80)
    run_ocr: bool = True
    ocr_language: str = Field(default="eng", pattern=r"^[a-zA-Z0-9_+]{1,48}$")
    alpha_matte_rgb: tuple[int, int, int] | None = None
    trace: TraceSettings = Field(default_factory=TraceSettings)

    @model_validator(mode="after")
    def validate_options(self):
        if not self.formats or len(set(self.formats)) != len(self.formats):
            raise ValueError("Choose at least one output format without duplicates.")
        if "ai" in self.formats and self.native_ai_mode != "desktop_handoff":
            raise ValueError("AI output requires native_ai_mode=desktop_handoff.")
        if self.native_ai_mode == "desktop_handoff" and "ai" not in self.formats:
            raise ValueError("desktop_handoff is only valid when AI output is selected.")
        if self.alpha_matte_rgb is not None and any(not 0 <= v <= 255 for v in self.alpha_matte_rgb):
            raise ValueError("Matte RGB values must be between 0 and 255.")
        return self


class BriefJobRequest(StrictModel):
    brief: str = Field(min_length=10, max_length=4000)
    front_notes: str = Field(default="", max_length=800)
    back_notes: str = Field(default="", max_length=800)
    mode: ProcessingMode = ProcessingMode.QUALITY
    dpi: Literal[330, 600] = 330
    formats: list[Literal["svg", "eps"]] = Field(default_factory=lambda: ["svg", "eps"])

    @model_validator(mode="after")
    def validate_formats(self):
        if not self.formats or len(set(self.formats)) != len(self.formats):
            raise ValueError("Choose at least one output format without duplicates.")
        return self


class JobState(StrictModel):
    job_id: str
    workflow: Workflow
    mode: ProcessingMode
    status: Literal["queued", "processing", "ready", "awaiting_desktop", "failed", "cancelled"]
    stage: str
    progress: int = Field(ge=0, le=100)
    remote_job_id: str
    upstream_key: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data.pop("upstream_key", None)
        if self.status in {"ready", "awaiting_desktop"}:
            data["downloads"] = {
                "bundle": f"/api/autofly/jobs/{self.job_id}/bundle",
                "file": f"/api/autofly/jobs/{self.job_id}/files/{{filename}}",
            }
        return data

