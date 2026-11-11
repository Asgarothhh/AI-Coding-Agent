"""Contract tests for lorne_py-trace-bug.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import app


class TraceBugTests(unittest.TestCase):
    def test_entrypoint_prints_bob(self) -> None:
        proc = subprocess.run(
            [sys.executable, "app.py"],
            cwd=HERE,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "Bob")

    def test_get_returns_the_requested_person(self) -> None:
        self.assertEqual(app.get("Ann")["name"], "Ann")


if __name__ == "__main__":
    unittest.main()
