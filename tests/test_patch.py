from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from harness.patch import (
    PatchAmbiguous,
    PatchError,
    PatchNotFound,
    apply_search_replace,
    apply_search_replace_file,
    closest_window,
    parse_search_replace,
)


class SearchReplaceTests(unittest.TestCase):
    def test_exact_single_match(self) -> None:
        result = apply_search_replace("def add(a, b):\n    return a - b\n", "a - b", "a + b")
        self.assertEqual(result.method, "exact")
        self.assertIn("a + b", result.text)

    def test_no_match_returns_closest(self) -> None:
        with self.assertRaises(PatchNotFound) as ctx:
            apply_search_replace("alpha beta gamma", "not present at all", "x")
        self.assertTrue(ctx.exception.closest)
        self.assertIn("alpha", closest_window("alpha beta gamma", "alp"))

    def test_repeated_match_is_ambiguous(self) -> None:
        with self.assertRaises(PatchAmbiguous):
            apply_search_replace("foo foo foo", "foo", "bar")

    def test_newline_normalization(self) -> None:
        haystack = "line1\r\nline2\r\n"
        result = apply_search_replace(haystack, "line1\nline2", "line1\nlineX")
        self.assertEqual(result.method, "newline")
        self.assertIn("lineX", result.text)

    def test_trailing_whitespace_normalization(self) -> None:
        haystack = "return a - b   \n"
        result = apply_search_replace(haystack, "return a - b", "return a + b")
        self.assertIn("return a + b", result.text)

    def test_indent_normalization(self) -> None:
        haystack = "def add(a, b):\n        return a - b\n"
        result = apply_search_replace(haystack, "    return a - b", "    return a + b")
        self.assertIn("return a + b", result.text)

    def test_file_apply_and_reject_test_and_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            work = task_dir / "workspace"
            work.mkdir(parents=True)
            target = work / "mylib.py"
            target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            (work / "test_mylib.py").write_text("assert True\n", encoding="utf-8")
            apply_search_replace_file(
                task_dir,
                "workspace/mylib.py",
                "a - b",
                "a + b",
                workdir=work,
            )
            self.assertIn("a + b", target.read_text(encoding="utf-8"))
            with self.assertRaises(PatchError):
                apply_search_replace_file(
                    task_dir,
                    "workspace/test_mylib.py",
                    "assert True",
                    "assert False",
                    workdir=work,
                )
            with self.assertRaises(PatchError):
                apply_search_replace_file(
                    task_dir,
                    "../secret.py",
                    "x",
                    "y",
                    workdir=work,
                )
            outside = Path(tmp) / "outside.py"
            outside.write_text("secret\n", encoding="utf-8")
            link = work / "escape.py"
            os.symlink(outside, link)
            with self.assertRaises(PatchError):
                apply_search_replace_file(
                    task_dir,
                    "workspace/escape.py",
                    "secret",
                    "leaked",
                    workdir=work,
                )

    def test_parse_search_replace_blocks(self) -> None:
        text = (
            "mylib.py\n"
            "<<<<<<< SEARCH\n"
            "return a - b\n"
            "=======\n"
            "return a + b\n"
            ">>>>>>> REPLACE\n"
        )
        hunks = parse_search_replace(text)
        self.assertEqual(len(hunks), 1)
        self.assertEqual(hunks[0].path, "mylib.py")
        self.assertEqual(hunks[0].search, "return a - b")
        self.assertEqual(hunks[0].replace, "return a + b")

    def test_second_apply_is_idempotent_failure_or_noop(self) -> None:
        original = "def add(a, b):\n    return a - b\n"
        result = apply_search_replace(original, "a - b", "a + b")
        self.assertIn("a + b", result.text)
        with self.assertRaises(PatchNotFound):
            apply_search_replace(result.text, "a - b", "a + b")


if __name__ == "__main__":
    unittest.main()
