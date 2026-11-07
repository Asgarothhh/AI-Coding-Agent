"""Thin optional adapter for openJiuwen / workswarm.

The MVP harness runs without either package.  When one of them is installed,
this module exposes a small protocol and a placeholder adapter that can later
be wired to the real agent runtime.  Nothing here imports heavy dependencies
at module import time.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .task import Task

__all__ = ["AgentRuntime", "PlaceholderRuntime", "build_runtime"]

log = logging.getLogger(__name__)

_CANDIDATE_MODULES = ("openjiuwen", "workswarm")


@runtime_checkable
class AgentRuntime(Protocol):
    name: str

    def available(self) -> bool: ...

    def solve(self, task: Task, workdir: Path) -> str | None: ...


@dataclass
class PlaceholderRuntime:
    """Adapter returned when a runtime package is importable but not wired."""

    name: str
    module_name: str
    module: Any = None

    def available(self) -> bool:
        return True

    def solve(self, task: Task, workdir: Path) -> str | None:
        raise NotImplementedError(
            f"{self.module_name} adapter is a placeholder; implement solve() for {task.task_id} in {workdir}"
        )


def build_runtime() -> AgentRuntime | None:
    """Return an adapter if openJiuwen/workswarm is installed, else ``None``.

    Import failures are treated as "not installed"; the stdlib harness remains
    fully functional.
    """

    for module_name in _CANDIDATE_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        except Exception as exc:  # pragma: no cover - defensive against broken installs
            log.warning("found %s but importing it failed: %s", module_name, exc)
            continue
        log.info("found optional runtime %s; using placeholder adapter", module_name)
        return PlaceholderRuntime(name=f"{module_name}-adapter", module_name=module_name, module=module)
    return None
