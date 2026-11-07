"""Task discovery and immutable task metadata.

Production code deliberately never reads the ``expected`` field from
``task.json``.  It is only consumed by the test-suite as a ground truth for
the bundled examples.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

KNOWN_TASK_TYPES = frozenset({"math", "search", "data_analysis", "coding"})
_REQUIRED_FIELDS = ("task_id", "task_type", "instruction", "reward", "check")


@dataclass(frozen=True, slots=True)
class Task:
    """A single challenge task rooted at ``task_dir``.

    ``workspace`` is the directory the instruction calls "the current
    directory".  Some tasks may not ship a workspace at all; in that case the
    task directory itself is used.
    """

    task_id: str
    task_type: str
    instruction: str
    reward: float
    check: str
    task_dir: Path
    workspace: Path | None = None

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


def load_task(task_dir: str | Path) -> Task:
    """Load one task directory.

    The JSON file is decoded with ``utf-8-sig`` so a byte-order mark does not
    break loading.  Unknown fields (including ``expected``) are ignored.
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
    try:
        reward = float(data["reward"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{task_json}: reward must be numeric") from exc
    check = str(data["check"]).strip().lower()

    workspace_path = path / "workspace"
    workspace = workspace_path if workspace_path.is_dir() else None

    return Task(
        task_id=task_id,
        task_type=task_type,
        instruction=instruction,
        reward=reward,
        check=check,
        task_dir=path,
        workspace=workspace,
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


__all__ = ["KNOWN_TASK_TYPES", "Task", "load_task", "load_tasks"]
