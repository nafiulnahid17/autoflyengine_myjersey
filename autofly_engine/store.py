from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from typing import Any

from .errors import AutoflyError
from .models import JobState, utc_now


class JobStore:
    def __init__(self, workspace: Path):
        self.root = workspace / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def _path(self, job_id: str) -> Path:
        return self.root / job_id / "state.json"

    def _write(self, state: JobState) -> None:
        path = self._path(state.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = state.model_dump_json(indent=2)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as stream:
            stream.write(payload)
            temporary = Path(stream.name)
        temporary.replace(path)

    def create(self, state: JobState) -> JobState:
        with self.lock:
            if self._path(state.job_id).exists():
                raise AutoflyError("JOB_EXISTS", "Job already exists.", 409)
            self._write(state)
            return state

    def get(self, job_id: str) -> JobState:
        path = self._path(job_id)
        if not path.is_file():
            raise AutoflyError("JOB_NOT_FOUND", "Autofly job not found.", 404)
        try:
            return JobState.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise AutoflyError("JOB_STATE_INVALID", "Stored job state is invalid.", 500) from exc

    def update(self, job_id: str, **updates: Any) -> JobState:
        with self.lock:
            state = self.get(job_id)
            updates["updated_at"] = utc_now()
            state = state.model_copy(update=updates)
            self._write(state)
            return state

    def active(self) -> list[JobState]:
        states: list[JobState] = []
        for path in self.root.glob("*/state.json"):
            try:
                state = JobState.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if state.status in {"queued", "processing"}:
                states.append(state)
        return states

