"""Thread-safe token/cost budget accounting.

The challenge proxy does not expose the remaining budget, so the harness
tracks it locally from every API response::

    cost = prompt_tokens / 10 + completion_tokens

Reasoning tokens are reported as completion tokens by the proxy and therefore
already count as output.  Cache accounting is ignored on purpose.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

__all__ = [
    "Budget",
    "BudgetExhausted",
    "BudgetSnapshot",
    "cost_of",
]

DEFAULT_PER_TASK_CAP = 3000.0


class BudgetExhausted(RuntimeError):
    """Raised when a new LLM call must not be started."""

    def __init__(
        self,
        message: str,
        *,
        task_id: str | None = None,
        remaining: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.remaining = remaining


def cost_of(prompt_tokens: int, completion_tokens: int) -> float:
    """Return challenge cost units for one API response."""

    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("token counts must be non-negative")
    return prompt_tokens / 10.0 + completion_tokens


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    total: float
    reserve: float
    per_task_cap: float
    spent: float
    remaining: float
    prompt_tokens: int
    completion_tokens: int
    stopped: bool


class Budget:
    """A shared budget for all agent sessions.

    ``check()`` must be called *before* an LLM request; ``charge()`` is called
    with the actual usage afterwards.  Charging never raises: overshooting the
    budget stops new work but lets responses already paid for finish.
    """

    def __init__(
        self,
        total: float,
        *,
        reserve: float = 0.0,
        per_task_cap: float = DEFAULT_PER_TASK_CAP,
    ) -> None:
        if total < 0:
            raise ValueError("budget total must be non-negative")
        if reserve < 0:
            raise ValueError("reserve must be non-negative")
        if per_task_cap <= 0:
            raise ValueError("per_task_cap must be positive")

        self._total = float(total)
        self._reserve = float(reserve)
        self._per_task_cap = float(per_task_cap)
        self._lock = threading.RLock()
        self._spent = 0.0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._per_task: dict[str, float] = {}
        self._stopped = False

    # -- read-only properties -------------------------------------------------
    @property
    def total(self) -> float:
        with self._lock:
            return self._total

    @property
    def reserve(self) -> float:
        with self._lock:
            return self._reserve

    @property
    def per_task_cap(self) -> float:
        with self._lock:
            return self._per_task_cap

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent

    @property
    def remaining(self) -> float:
        with self._lock:
            return self._total - self._spent

    @property
    def prompt_tokens(self) -> int:
        with self._lock:
            return self._prompt_tokens

    @property
    def completion_tokens(self) -> int:
        with self._lock:
            return self._completion_tokens

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._stopped or (self._total - self._spent) <= 0.0

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    def can_start_task(self) -> bool:
        """True when starting another task is still allowed."""

        with self._lock:
            return not self._stopped and (self._total - self._spent) > self._reserve

    def task_spent(self, task_id: str) -> float:
        with self._lock:
            return self._per_task.get(task_id, 0.0)

    # -- mutation -------------------------------------------------------------
    def check(
        self,
        task_id: str | None = None,
        *,
        estimated_cost: float = 0.0,
    ) -> float:
        """Validate that an LLM call may start and return remaining budget.

        Raises :class:`BudgetExhausted` when the global reserve is reached or
        when the per-task cap would be exceeded.
        """

        with self._lock:
            remaining = self._total - self._spent
            if self._stopped:
                if remaining <= self._reserve:
                    raise BudgetExhausted(
                        f"budget reserve reached (remaining={remaining:.2f}, reserve={self._reserve:.2f})",
                        task_id=task_id,
                        remaining=remaining,
                    )
                raise BudgetExhausted(
                    "budget is stopped",
                    task_id=task_id,
                    remaining=remaining,
                )
            if remaining <= self._reserve:
                self._stopped = True
                raise BudgetExhausted(
                    f"budget reserve reached (remaining={remaining:.2f}, reserve={self._reserve:.2f})",
                    task_id=task_id,
                    remaining=remaining,
                )
            if task_id is not None and estimated_cost > 0:
                used = self._per_task.get(task_id, 0.0)
                if used + estimated_cost > self._per_task_cap:
                    raise BudgetExhausted(
                        (
                            f"per-task cap would be exceeded for {task_id} "
                            f"(used={used:.2f}, estimate={estimated_cost:.2f}, cap={self._per_task_cap:.2f})"
                        ),
                        task_id=task_id,
                        remaining=remaining,
                    )
            return remaining

    def charge(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        task_id: str | None = None,
    ) -> float:
        """Charge actual usage and return the cost in challenge units."""

        cost = cost_of(prompt_tokens, completion_tokens)
        with self._lock:
            self._spent += cost
            self._prompt_tokens += int(prompt_tokens)
            self._completion_tokens += int(completion_tokens)
            if task_id is not None:
                self._per_task[task_id] = self._per_task.get(task_id, 0.0) + cost
            if (self._total - self._spent) <= self._reserve:
                self._stopped = True
            return cost

    def stop(self) -> None:
        """Prevent any further LLM calls."""

        with self._lock:
            self._stopped = True

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return BudgetSnapshot(
                total=self._total,
                reserve=self._reserve,
                per_task_cap=self._per_task_cap,
                spent=self._spent,
                remaining=self._total - self._spent,
                prompt_tokens=self._prompt_tokens,
                completion_tokens=self._completion_tokens,
                stopped=self._stopped,
            )
