"""Solver package exports.

Keeping this module light avoids import cycles: individual solvers are
imported directly by the runner.
"""

from __future__ import annotations

from .base import SolveResult, SolverContext, SolverOptions

__all__ = ["SolveResult", "SolverContext", "SolverOptions"]
