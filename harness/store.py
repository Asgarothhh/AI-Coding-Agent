"""Atomic answer and solution persistence.

Answers are written next to ``task.json`` and, when present, into
``workspace/``.  Writes use a temp file in the destination directory followed
by ``fsync`` + ``os.replace`` so a crash cannot leave a half-written answer.

``solutions.json`` is rewritten after every solution and fsynced, which makes
per-solution progress durable even if the container is killed.

``predictions.jsonl`` is diagnostic only: one JSON line per task, appended and
fsynced right after the initial estimates are built (including ``--dry-run``
when ``$OUT`` is writable).  It never affects ``answer.txt``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .task import Task

__all__ = [
    "AnswerStore",
    "atomic_write_bytes",
    "atomic_write_text",
    "estimate_theory",
    "normalize_answer",
]

_DEFAULT_OUT = "/tmp/openjiuwen-out"


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform dependent
        return
    try:
        os.fsync(directory_fd)
    except OSError:  # pragma: no cover - e.g. some network filesystems
        pass
    finally:
        os.close(directory_fd)


def atomic_write_bytes(path: str | Path, data: bytes, *, mode: int = 0o644) -> Path:
    """Atomically replace ``path`` with ``data`` and fsync it."""

    destination = Path(path)
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)

    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(parent),
    )
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, destination)
        _fsync_directory(parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return destination


def atomic_write_text(path: str | Path, text: str, *, mode: int = 0o644) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def estimate_theory(estimate: Any) -> str:
    """Name the estimator path behind one forecast.

    ``deterministic`` means no LLM call is planned, ``heuristic`` means the
    prompt-character fallback was used, ``calibration+online`` is a calibrated
    prior adjusted by observed usage, and ``calibration`` is the plain prior.
    """

    drivers = tuple(str(driver) for driver in (getattr(estimate, "drivers", ()) or ()))
    if "deterministic_solver=true" in drivers:
        return "deterministic"
    if any(driver.startswith("scope=heuristic") for driver in drivers):
        return "heuristic"
    if any(driver.startswith("online_samples=") for driver in drivers):
        return "calibration+online"
    return "calibration"


def normalize_answer(answer: str | None) -> str:
    """Return one plain-text line as required by the challenge contract."""

    if answer is None:
        raise ValueError("answer is None")
    lines = [line.strip() for line in str(answer).splitlines() if line.strip()]
    if not lines:
        raise ValueError("answer is empty")
    return lines[0]


class AnswerStore:
    def __init__(self, out: str | Path | None = None, *, verbose: bool = False) -> None:
        raw = Path(out) if out is not None else Path(os.environ.get("OUT", _DEFAULT_OUT))
        if raw.exists() and raw.is_dir():
            self._out_dir = raw
            self._solutions_path = raw / "solutions.json"
        elif raw.exists() and raw.is_file():
            self._out_dir = raw.parent
            self._solutions_path = raw
        elif raw.suffix.lower() == ".json":
            self._out_dir = raw.parent
            self._solutions_path = raw
        else:
            self._out_dir = raw
            self._solutions_path = raw / "solutions.json"

        self._verbose = verbose
        self._records: list[dict[str, Any]] = []
        self._predictions_path = self._out_dir / "predictions.jsonl"
        self._lock = threading.RLock()

    @property
    def out_dir(self) -> Path:
        return self._out_dir

    @property
    def solutions_path(self) -> Path:
        return self._solutions_path

    @property
    def trace_path(self) -> Path:
        return self._out_dir / "trace.jsonl"

    @property
    def predictions_path(self) -> Path:
        return self._predictions_path

    def record_prediction(
        self,
        task: Task,
        estimate: Any,
        model: str | None = None,
        solver: str | None = None,
    ) -> dict[str, Any]:
        """Append one fsynced JSON line to ``predictions.jsonl``.

        Called once per task right after the initial estimates are built, so
        later replans never add duplicate lines.  The write is intentionally
        appending rather than atomic-replace: a crash may lose the last line
        but never the file, and the record stays streaming-friendly.
        """

        drivers = [str(driver) for driver in (getattr(estimate, "drivers", ()) or ())]
        record: dict[str, Any] = {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "solver": str(solver if solver is not None else getattr(estimate, "solver", "")),
            "model": str(model if model is not None else getattr(estimate, "model", "")),
            "expected_cost": float(getattr(estimate, "expected_cost", 0.0) or 0.0),
            "upper_cost": float(getattr(estimate, "upper_cost", 0.0) or 0.0),
            "confidence": str(getattr(estimate, "confidence", "low")),
            "drivers": drivers,
            "theory": estimate_theory(estimate),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            self._predictions_path.parent.mkdir(parents=True, exist_ok=True)
            with self._predictions_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        return record

    def write_answer(self, task: Task, answer: str) -> tuple[Path, ...]:
        """Write the normalised answer to every contract location."""

        line = normalize_answer(answer)
        payload = (line + "\n").encode("utf-8")
        written: list[Path] = []
        with self._lock:
            for location in task.answer_locations():
                written.append(atomic_write_bytes(location, payload))
        return tuple(written)

    def record_solution(
        self,
        task: Task,
        result: Any,
        status: str,
        duration: float,
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": task.task_id,
            "task_type": task.task_type,
            "reward": task.reward,
            "check": task.check,
            "status": status,
            "solver": str(getattr(result, "solver", "") or ""),
            "answer": getattr(result, "answer", None),
            "tokens": float(getattr(result, "tokens", 0.0) or 0.0),
            "duration": round(float(duration), 6),
            "notes": str(getattr(result, "notes", "") or ""),
            "error": error,
            "answer_paths": [str(path) for path in task.answer_locations()],
        }
        with self._lock:
            self._records.append(record)
            self.flush()
        return record

    def flush(self) -> Path:
        with self._lock:
            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "count": len(self._records),
                "solutions": list(self._records),
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            return atomic_write_text(self._solutions_path, text)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records)
