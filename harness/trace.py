"""JSONL execution trace.

Each completed task appends one line and fsyncs it, so the trace is useful
even when the process is killed by the one-hour limit.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["TraceWriter"]


class TraceWriter:
    def __init__(self, path: str | Path | None = None, *, verbose: bool = False) -> None:
        self._path = Path(path) if path is not None else None
        self._verbose = verbose
        self._lock = threading.Lock()

    @property
    def path(self) -> Path | None:
        return self._path

    def write(
        self,
        *,
        task_id: str,
        task_type: str,
        reward: float,
        status: str,
        solver: str,
        tokens: float,
        duration: float,
        error: str | None = None,
        **extra: Any,
    ) -> None:
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "type": task_type,
            "reward": reward,
            "status": status,
            "solver": solver,
            "tokens": round(float(tokens), 6),
            "duration": round(float(duration), 6),
            "error": error,
        }
        record.update(extra)

        if self._path is None:
            return

        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())

        if self._verbose:
            print(
                f"[trace] {task_id} status={status} solver={solver} tokens={tokens:.3f}",
                file=sys.stderr,
            )
