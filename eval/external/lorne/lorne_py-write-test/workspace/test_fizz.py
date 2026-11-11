"""Behavioral unittest suite for fizz.py.

Converted from the original lorne ``py-write-test`` checker: the weak
pytest/grep check is replaced by explicit assertions against ``fizzbuzz``.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fizz import fizzbuzz


class FizzBuzzTests(unittest.TestCase):
    def test_plain_numbers(self) -> None:
        self.assertEqual(fizzbuzz(1), "1")
        self.assertEqual(fizzbuzz(2), "2")
        self.assertEqual(fizzbuzz(7), "7")

    def test_multiples_of_three(self) -> None:
        self.assertEqual(fizzbuzz(3), "fizz")
        self.assertEqual(fizzbuzz(6), "fizz")

    def test_multiples_of_five(self) -> None:
        self.assertEqual(fizzbuzz(5), "buzz")
        self.assertEqual(fizzbuzz(10), "buzz")

    def test_multiples_of_fifteen(self) -> None:
        self.assertEqual(fizzbuzz(15), "fizzbuzz")
        self.assertEqual(fizzbuzz(30), "fizzbuzz")


if __name__ == "__main__":
    unittest.main()
