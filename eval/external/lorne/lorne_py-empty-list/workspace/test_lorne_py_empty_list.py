"""Contract tests for lorne_py-empty-list.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from stats import average


class EmptyListTests(unittest.TestCase):
    def test_average_of_empty_list_is_zero(self) -> None:
        self.assertEqual(average([]), 0.0)

    def test_average_of_non_empty_list(self) -> None:
        self.assertEqual(average([1, 3]), 2.0)


if __name__ == "__main__":
    unittest.main()
