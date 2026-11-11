"""Contract tests for lorne_py-argparse-flag.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=HERE,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class ArgparseShoutTests(unittest.TestCase):
    def test_shout_prints_uppercase_greeting(self) -> None:
        proc = _run(sys.executable, "cli.py", "--name", "bob", "--shout")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "HELLO, BOB!")

    def test_default_output_is_unchanged(self) -> None:
        proc = _run(sys.executable, "cli.py", "--name", "bob")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "Hello, bob!")


if __name__ == "__main__":
    unittest.main()
