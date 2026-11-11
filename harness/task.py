"""Task discovery and immutable task metadata.

Production code deliberately never reads the ``expected`` field from
``task.json``.  It is only consumed by the test-suite as a ground truth for
the bundled examples.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KNOWN_TASK_TYPES = frozenset({"math", "search", "data_analysis", "coding"})
_REQUIRED_FIELDS = ("task_id", "task_type", "instruction", "reward", "check")


@dataclass(frozen=True, slots=True)
class Task:
    """A single challenge task rooted at ``task_dir``.

    ``workspace`` is the directory the instruction calls "the current
    directory".  Some tasks may not ship a workspace at all; in that case the
    task directory itself is used.

    ``raw`` is the full decoded ``task.json`` object, including unknown keys
    and ``expected``.  Production solvers must not treat ``expected`` as an
    answer.
    """

    task_id: str
    task_type: str
    instruction: str
    reward: float
    check: str
    task_dir: Path
    workspace: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def workdir(self) -> Path:
        return self.workspace if self.workspace is not None else self.task_dir

    @property
    def has_workspace(self) -> bool:
        return self.workspace is not None

    def answer_locations(self) -> tuple[Path, ...]:
        """Every place an ``answer.txt`` must be written.

        The official contract requires the answer next to ``task.json``;
        writing it into ``workspace/`` as well is a belt-and-braces fallback
        for checkers that look at the working directory.
        """

        locations = [self.task_dir / "answer.txt"]
        if self.workspace is not None:
            try:
                same = self.workspace.resolve() == self.task_dir.resolve()
            except OSError:  # pragma: no cover - exotic filesystem failure
                same = False
            if not same:
                locations.append(self.workspace / "answer.txt")
        return tuple(locations)


def resolve_task_path(task_dir: str | Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` so it stays inside ``task_dir``.

    Rejects absolute paths, ``..`` traversal, and symlink escape.  The
    returned path is the resolved real path of an existing or not-yet-created
    location whose parent chain remains inside the task directory.
    """

    root = Path(task_dir).resolve()
    raw = Path(candidate)
    if raw.is_absolute():
        raise ValueError(f"absolute path outside task directory: {candidate}")
    parts = raw.parts
    if any(part == ".." for part in parts):
        raise ValueError(f"path traversal is not allowed: {candidate}")
    target = (root / raw).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes task directory: {candidate}") from exc
    return target


def _safe_workspace(task_dir: Path) -> Path | None:
    workspace_path = task_dir / "workspace"
    if not workspace_path.is_dir():
        return None
    try:
        resolve_task_path(task_dir, "workspace")
    except ValueError:
        log.warning("ignoring workspace that escapes task directory %s", task_dir)
        return None
    return workspace_path


def load_task(task_dir: str | Path) -> Task:
    """Load one task directory.

    The JSON file is decoded with ``utf-8-sig`` so a byte-order mark does not
    break loading.  Unknown fields are kept in ``Task.raw``.  Production code
    still does not use ``expected`` as an answer.
    """

    path = Path(task_dir)
    task_json = path / "task.json"
    if not task_json.is_file():
        raise FileNotFoundError(f"task.json not found in {path}")

    with task_json.open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{task_json} must contain a JSON object")

    missing = [field for field in _REQUIRED_FIELDS if field not in data]
    if missing:
        raise ValueError(f"{task_json} is missing required field(s): {', '.join(missing)}")

    task_id = str(data["task_id"]).strip() or path.name
    task_type = str(data["task_type"]).strip().lower()
    instruction = str(data["instruction"])
    raw_reward = data["reward"]
    if isinstance(raw_reward, bool) or not isinstance(raw_reward, (int, float)):
        raise ValueError(f"{task_json}: reward must be numeric")
    reward = float(raw_reward)
    if not math.isfinite(reward):
        raise ValueError(f"{task_json}: reward must be finite")
    check = str(data["check"]).strip().lower()

    workspace = _safe_workspace(path)

    return Task(
        task_id=task_id,
        task_type=task_type,
        instruction=instruction,
        reward=reward,
        check=check,
        task_dir=path,
        workspace=workspace,
        raw=dict(data),
    )


def load_tasks(tasks_dir: str | Path) -> list[Task]:
    """Discover tasks below ``tasks_dir``.

    A directory is a task only when it directly contains ``task.json``.  A
    missing ``workspace/`` child is normal and must not fail loading.
    Invalid task directories are logged and skipped so one broken task cannot
    sink a whole run.
    """

    root = Path(tasks_dir)
    if not root.exists():
        raise FileNotFoundError(f"tasks directory does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"tasks path is not a directory: {root}")

    # Also allow pointing directly at a single task directory.
    if (root / "task.json").is_file():
        return [load_task(root)]

    tasks: list[Task] = []
    seen: set[str] = set()
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or not (child / "task.json").is_file():
            continue
        try:
            task = load_task(child)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.warning("skipping invalid task directory %s: %s", child, exc)
            continue
        if task.task_id in seen:
            log.warning("skipping duplicate task_id %s in %s", task.task_id, child)
            continue
        seen.add(task.task_id)
        tasks.append(task)
    return tasks


__all__ = ["KNOWN_TASK_TYPES", "Task", "load_task", "load_tasks", "resolve_task_path"]
