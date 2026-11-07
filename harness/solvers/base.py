"""Common solver interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..budget import Budget
from ..llm import LLMClient
from ..task import Task

__all__ = ["SolveResult", "Solver", "SolverContext", "SolverOptions"]


@dataclass(frozen=True, slots=True)
class SolverOptions:
    max_iterations: int = 6
    mutation_repair: bool = False
    test_timeout: float = 60.0
    mutation_time_budget: float = 20.0
    max_tokens: int = 1024
    verbose: bool = False


@dataclass(slots=True)
class SolverContext:
    budget: Budget
    llm: LLMClient | None = None
    options: SolverOptions = field(default_factory=SolverOptions)


@dataclass(frozen=True, slots=True)
class SolveResult:
    answer: str | None
    tokens: float = 0.0
    solver: str = ""
    notes: str = ""

    @property
    def solved(self) -> bool:
        return self.answer is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "answer": self.answer,
            "tokens": round(self.tokens, 6),
            "solver": self.solver,
            "notes": self.notes,
        }


class Solver(Protocol):
    def __call__(self, task: Task, ctx: SolverContext) -> SolveResult: ...
