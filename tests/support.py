"""Test support helpers (stdlib only)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from harness.budget import Budget
from harness.solvers.base import SolverContext, SolverOptions
from harness.task import Task, load_task

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "task-dir"


def expected_for(task_id: str) -> str:
    """Test-only access to the official expected answer."""

    data = json.loads((EXAMPLES / task_id / "task.json").read_text(encoding="utf-8-sig"))
    return str(data["expected"])


def copy_examples(destination: str | Path) -> Path:
    destination = Path(destination)
    shutil.copytree(EXAMPLES, destination)
    for answer in destination.rglob("answer.txt"):
        answer.unlink()
    return destination


def make_task(
    base: str | Path,
    task_id: str,
    *,
    task_type: str = "math",
    instruction: str = "Compute 2 + 2 and write the answer.",
    files: dict[str, str] | None = None,
    workspace: bool = True,
    reward: float = 1.0,
    check: str = "exact_int",
) -> Task:
    task_dir = Path(base) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": task_id,
        "task_type": task_type,
        "instruction": instruction,
        "reward": reward,
        "check": check,
    }
    (task_dir / "task.json").write_text(json.dumps(payload), encoding="utf-8")
    if workspace:
        workdir = task_dir / "workspace"
        workdir.mkdir(exist_ok=True)
        for relative, content in (files or {}).items():
            target = workdir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    return load_task(task_dir)


def make_context(
    *,
    budget: Budget | None = None,
    llm: Any = None,
    **options: Any,
) -> SolverContext:
    return SolverContext(
        budget=budget or Budget(100_000.0),
        llm=llm,
        options=SolverOptions(**options),
    )
