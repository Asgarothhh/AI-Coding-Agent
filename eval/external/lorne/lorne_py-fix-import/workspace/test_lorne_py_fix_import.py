"""Contract tests for lorne_py-fix-import.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class ImportPathTests(unittest.TestCase):
    def test_main_prints_hello_world(self) -> None:
        proc = subprocess.run(
            [sys.executable, "main.py"],
            cwd=HERE,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "hello-world")


if __name__ == "__main__":
    unittest.main()
