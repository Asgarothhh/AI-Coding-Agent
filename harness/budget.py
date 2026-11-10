"""Thread-safe token/cost budget accounting for one shared budget.

The challenge exposes a single global budget; there is deliberately **no**
per-task cap.  Every LLM call must reserve its worst-case cost before it is
sent and settle the reservation against the actual usage afterwards::

    cost = input_tokens / 10 + output_tokens

The ledger keeps ``spent + reserved <= total`` for every granted reservation.
Settling may charge more than the estimate (a response cannot be un-sent);
after that, subsequent reservations see the real remaining budget.

Budget resolution from the environment lives in :class:`BudgetProvider` so the
startup path has exactly one place that can invent a number.
"""

from __future__ import annotations

import math
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .usage import TokenUsage, normalize_usage

__all__ = [
    "DEFAULT_OFFLINE_BUDGET",
    "Budget",
    "BudgetConfigurationError",
    "BudgetDecision",
    "BudgetError",
    "BudgetExhausted",
    "BudgetLedger",
    "BudgetProvider",
    "BudgetSnapshot",
    "Reservation",
    "cost_of",
]

DEFAULT_OFFLINE_BUDGET = 100_000.0
_OFFLINE_WARNING = (
    "warning: no token budget configured; --offline uses {total:g} cost units. "
    "This is a local test assumption, not a competition budget value."
)


class BudgetError(RuntimeError):
    """Base class for ledger misuse and bookkeeping failures."""


class BudgetExhausted(BudgetError):
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


class BudgetConfigurationError(BudgetError):
    """Raised when no trustworthy total budget can be resolved at startup."""


def cost_of(prompt_tokens: int, completion_tokens: int) -> float:
    """Return challenge cost units for one API response."""

    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("token counts must be non-negative")
    return prompt_tokens / 10.0 + completion_tokens


def _finite_non_negative(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return number


def _coerce_usage(usage: TokenUsage | Mapping[str, Any] | Any) -> TokenUsage:
    """Accept the canonical record, a raw mapping or a duck-typed usage."""

    if isinstance(usage, TokenUsage):
        return usage
    if usage is None or isinstance(usage, Mapping):
        return normalize_usage(usage)
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None or output_tokens is None:
        raise BudgetError(
            f"unsupported usage object: {type(usage).__name__}; "
            "expected TokenUsage or a raw usage mapping"
        )
    return TokenUsage(
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        reasoning_tokens=int(getattr(usage, "reasoning_tokens", 0) or 0),
        total_cost=float(getattr(usage, "total_cost", 0.0) or 0.0),
        source_fields=tuple(getattr(usage, "source_fields", ()) or ()),
    )


@dataclass(frozen=True, slots=True)
class Reservation:
    """Handle returned by :meth:`BudgetLedger.reserve`."""

    request_id: str
    amount: float
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    safety_margin: float = 0.0


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    total: float
    spent: float
    reserved: float
    remaining: float
    prompt_tokens: int
    completion_tokens: int
    stopped: bool


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    total: float
    source: str


class BudgetProvider:
    """Resolve the total budget once, at startup, with an auditable source.

    Priority: CLI ``--budget`` > ``OJ_TOKEN_BUDGET`` > ``BUDGET``.  When none
    of them is present the provider refuses to guess unless the caller is
    explicitly in ``--offline`` mode, where a local test default is safe.
    """

    _ENV_ORDER = ("OJ_TOKEN_BUDGET", "BUDGET")

    def __init__(
        self,
        cli_budget: float | None = None,
        *,
        offline: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._cli_budget = cli_budget
        self._offline = offline
        self._environ: Mapping[str, str] = (
            os.environ if environ is None else environ
        )

    def resolve(self) -> BudgetDecision:
        if self._cli_budget is not None:
            return BudgetDecision(
                total=self._parse("<CLI --budget>", self._cli_budget),
                source="cli",
            )

        for name in self._ENV_ORDER:
            raw = self._environ.get(name)
            if raw is None or not str(raw).strip():
                continue
            return BudgetDecision(
                total=self._parse(f"${name}", raw),
                source=f"env:{name}",
            )

        if self._offline:
            print(
                _OFFLINE_WARNING.format(total=DEFAULT_OFFLINE_BUDGET),
                file=sys.stderr,
            )
            return BudgetDecision(
                total=DEFAULT_OFFLINE_BUDGET,
                source="offline-default",
            )

        raise BudgetConfigurationError(
            "no token budget configured: pass --budget, set OJ_TOKEN_BUDGET "
            "(or BUDGET), or run with --offline for the local test default. "
            "Refusing to invent a competition budget."
        )

    @staticmethod
    def _parse(origin: str, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise BudgetConfigurationError(
                f"invalid budget from {origin}: {raw!r} is not a number"
            ) from exc
        if not math.isfinite(value) or value < 0:
            raise BudgetConfigurationError(
                f"invalid budget from {origin}: {raw!r} must be a finite "
                "non-negative number"
            )
        return value


class BudgetLedger:
    """Atomic reserve/settle/release ledger for the single global budget.

    All state transitions happen under one ``RLock``.  A reservation is
    deducted from the available budget immediately, so two concurrent workers
    can never reserve the same cost units.
    """

    def __init__(self, total: float, *, reserve: float = 0.0) -> None:
        self._total = _finite_non_negative("budget total", total)
        self._reserve = _finite_non_negative("reserve", reserve)
        if self._reserve > self._total:
            raise ValueError("reserve must not exceed the total budget")

        self._lock = threading.RLock()
        self._spent = 0.0
        self._reserved = 0.0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._stopped = False
        self._active: dict[str, Reservation] = {}
        self._closed: dict[str, str] = {}

    # -- read-only properties -------------------------------------------------
    @property
    def total(self) -> float:
        with self._lock:
            return self._total

    @property
    def reserve_amount(self) -> float:
        with self._lock:
            return self._reserve

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent

    @property
    def reserved(self) -> float:
        with self._lock:
            return self._reserved

    @property
    def remaining(self) -> float:
        with self._lock:
            return self._remaining_locked()

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
            return self._stopped or self._remaining_locked() <= 0.0

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    def _available_locked(self) -> float:
        return (
            self._total
            - self._reserve
            - self._spent
            - self._reserved
        )

    def _remaining_locked(self) -> float:
        return self._total - self._spent - self._reserved

    def can_start_task(self) -> bool:
        """True when another task may start under the global budget."""

        with self._lock:
            return not self._stopped and self._available_locked() > 0.0

    # -- reservation lifecycle ------------------------------------------------
    def reserve(
        self,
        request_id: str,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        *,
        safety_margin: float = 0.0,
    ) -> Reservation:
        """Deduct a worst-case estimate and return the reservation handle.

        Raises :class:`BudgetExhausted` when the estimate does not fit; the
        check and the deduction are one atomic operation.
        """

        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        if estimated_input_tokens < 0 or estimated_output_tokens < 0:
            raise ValueError("estimated token counts must be non-negative")
        margin = _finite_non_negative("safety_margin", safety_margin)
        amount = (
            estimated_input_tokens / 10.0
            + estimated_output_tokens
            + margin
        )

        with self._lock:
            if request_id in self._active:
                raise BudgetError(
                    f"reservation {request_id!r} is already active"
                )
            if request_id in self._closed:
                state = self._closed[request_id]
                raise BudgetError(
                    f"reservation {request_id!r} was already {state}; "
                    "use a new request id"
                )
            if self._stopped:
                raise BudgetExhausted(
                    (
                        f"budget is stopped (remaining={self._remaining_locked():.2f}, "
                        f"reserve={self._reserve:.2f})"
                    ),
                    remaining=self._remaining_locked(),
                )

            available = self._available_locked()
            if amount > available:
                raise BudgetExhausted(
                    (
                        f"insufficient budget for {request_id!r}: "
                        f"estimate={amount:.2f} available={available:.2f} "
                        f"(total={self._total:.2f}, spent={self._spent:.2f}, "
                        f"reserved={self._reserved:.2f}, "
                        f"reserve={self._reserve:.2f})"
                    ),
                    remaining=self._remaining_locked(),
                )

            reservation = Reservation(
                request_id=request_id,
                amount=amount,
                estimated_input_tokens=int(estimated_input_tokens),
                estimated_output_tokens=int(estimated_output_tokens),
                safety_margin=margin,
            )
            self._active[request_id] = reservation
            self._reserved += amount
            return reservation

    def settle(
        self,
        reservation: Reservation | str,
        usage: TokenUsage | Mapping[str, Any] | None,
    ) -> float:
        """Release a reservation and charge the actual usage.

        The actual cost may exceed the estimate; that is allowed because the
        request is already paid for.  Subsequent reservations observe the
        reduced remaining budget.  Returns the charged cost.
        """

        request_id = self._request_id(reservation)
        usage_record = _coerce_usage(usage)
        actual = (
            usage_record.total_cost
            if usage_record.total_cost > 0
            else usage_record.cost
        )
        if not math.isfinite(actual) or actual < 0:
            raise BudgetError(
                f"invalid usage cost for {request_id!r}: {actual!r}"
            )

        with self._lock:
            active = self._active.get(request_id)
            if active is None:
                self._raise_unknown(request_id, "settle")
            assert active is not None  # for type checkers
            self._reserved -= active.amount
            self._active.pop(request_id, None)
            self._closed[request_id] = "settled"
            self._spent += actual
            self._prompt_tokens += usage_record.input_tokens
            self._completion_tokens += usage_record.output_tokens
            if self._remaining_locked() <= self._reserve:
                self._stopped = True
            return actual

    def release(self, reservation: Reservation | str) -> float:
        """Release a reservation without charging (pre-call failure)."""

        request_id = self._request_id(reservation)
        with self._lock:
            active = self._active.get(request_id)
            if active is None:
                self._raise_unknown(request_id, "release")
            assert active is not None
            self._reserved -= active.amount
            self._active.pop(request_id, None)
            self._closed[request_id] = "released"
            return active.amount

    # -- legacy advisory API (used by solvers) --------------------------------
    def check(
        self,
        task_id: str | None = None,
        *,
        estimated_cost: float = 0.0,
    ) -> float:
        """Advisory pre-call check against the global remaining budget.

        Kept for callers that charge after the response instead of reserving.
        It never mutates budget state; it returns the available budget or
        raises :class:`BudgetExhausted`.
        """

        estimate = _finite_non_negative("estimated_cost", estimated_cost)
        with self._lock:
            available = self._available_locked()
            if self._stopped:
                raise BudgetExhausted(
                    (
                        f"budget is stopped (remaining={self._remaining_locked():.2f}, "
                        f"reserve={self._reserve:.2f})"
                    ),
                    task_id=task_id,
                    remaining=self._remaining_locked(),
                )
            if available <= 0.0:
                raise BudgetExhausted(
                    (
                        f"budget exhausted (remaining={self._remaining_locked():.2f}, "
                        f"reserve={self._reserve:.2f})"
                    ),
                    task_id=task_id,
                    remaining=self._remaining_locked(),
                )
            if estimate > available:
                raise BudgetExhausted(
                    (
                        f"insufficient budget (estimate={estimate:.2f}, "
                        f"available={available:.2f}, "
                        f"remaining={self._remaining_locked():.2f}, "
                        f"reserve={self._reserve:.2f})"
                    ),
                    task_id=task_id,
                    remaining=self._remaining_locked(),
                )
            return available

    def charge(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        task_id: str | None = None,
    ) -> float:
        """Charge actual usage directly and return the cost.

        ``task_id`` is accepted for backwards compatibility with solvers and
        ignored: per-task budgets are not part of the contract.  Charging never
        raises, so a response that was already paid for can finish.
        """

        del task_id
        cost = cost_of(prompt_tokens, completion_tokens)
        with self._lock:
            self._spent += cost
            self._prompt_tokens += int(prompt_tokens)
            self._completion_tokens += int(completion_tokens)
            if self._remaining_locked() <= self._reserve:
                self._stopped = True
            return cost

    def stop(self) -> None:
        """Prevent any further LLM calls (sticky)."""

        with self._lock:
            self._stopped = True

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return BudgetSnapshot(
                total=self._total,
                spent=self._spent,
                reserved=self._reserved,
                remaining=self._remaining_locked(),
                prompt_tokens=self._prompt_tokens,
                completion_tokens=self._completion_tokens,
                stopped=self._stopped,
            )

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _request_id(handle: Reservation | str) -> str:
        if isinstance(handle, Reservation):
            return handle.request_id
        if isinstance(handle, str) and handle.strip():
            return handle
        raise BudgetError(f"invalid reservation handle: {handle!r}")

    def _raise_unknown(self, request_id: str, action: str) -> None:
        state = self._closed.get(request_id)
        if state is not None:
            raise BudgetError(
                f"cannot {action} reservation {request_id!r}: it was already "
                f"{state}"
            )
        raise BudgetError(f"unknown reservation {request_id!r}")


# ``Budget`` is the historical name used by solvers and tests.  It is the same
# ledger; keeping the alias avoids touching unrelated modules.
Budget = BudgetLedger
