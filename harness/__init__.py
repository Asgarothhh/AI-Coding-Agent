"""Budget-aware MVP harness for the openJiuwen Challenge 2026.

The core package is intentionally stdlib-only.  Optional agent runtimes
(openJiuwen / workswarm) are discovered lazily by
:mod:`harness.ojiuwen_runtime` and are never a hard dependency.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
