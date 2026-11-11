"""Contract tests for lorne_py-fix-add.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from calc import add


class AddTests(unittest.TestCase):
    def test_add_adds(self) -> None:
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
