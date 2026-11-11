"""Contract tests for lorne_py-docstring-ast.

Reproduces the original lorne ``check.sh`` assertions with stdlib only.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class AreaAnnotationTests(unittest.TestCase):
    def test_area_has_annotations_and_docstring(self) -> None:
        source = (HERE / "geometry.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "area"
            ),
            None,
        )
        self.assertIsNotNone(function, "function area not found")
        assert function is not None  # narrowed for type checkers

        for argument in function.args.args:
            self.assertIsNotNone(
                argument.annotation,
                f"missing annotation for {argument.arg}",
            )
        self.assertIsInstance(function.returns, ast.Name)
        self.assertEqual(getattr(function.returns, "id", None), "float")
        self.assertTrue(ast.get_docstring(function), "missing docstring")


if __name__ == "__main__":
    unittest.main()
