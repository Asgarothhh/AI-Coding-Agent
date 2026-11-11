"""Contract tests for lorne_py-rename-func.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.  The
old-name scan deliberately skips this wrapper so the test file cannot match
itself.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import utils


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=HERE,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class RenameFunctionTests(unittest.TestCase):
    def test_renamed_function_is_available(self) -> None:
        self.assertEqual(utils.total([1, 2, 3]), 6)
        self.assertFalse(hasattr(utils, "calc_total"))

    def test_entrypoints_still_work(self) -> None:
        main = _run(sys.executable, "main.py")
        self.assertEqual(main.returncode, 0, main.stderr)
        self.assertEqual(main.stdout.strip(), "6")

        report = _run(sys.executable, "report.py")
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertEqual(report.stdout.strip(), "total=9")

    def test_old_name_is_absent_from_the_workspace(self) -> None:
        wrapper = Path(__file__).resolve()
        for path in sorted(HERE.rglob("*")):
            if not path.is_file() or path.resolve() == wrapper:
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            self.assertNotIn(
                "calc_total",
                text,
                f"old name calc_total still present in {path.name}",
            )


if __name__ == "__main__":
    unittest.main()
