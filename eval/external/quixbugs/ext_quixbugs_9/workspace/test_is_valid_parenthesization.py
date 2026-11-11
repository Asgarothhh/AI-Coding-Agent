"""QuixBugs-derived tests for is_valid_parenthesization."""

import unittest

from is_valid_parenthesization import is_valid_parenthesization


class TestIsValidParenthesization(unittest.TestCase):
    def test_case_01(self):
        self.assertEqual(is_valid_parenthesization('((()()))()'), True)

    def test_case_02(self):
        self.assertEqual(is_valid_parenthesization(')()('), False)

    def test_case_03(self):
        self.assertEqual(is_valid_parenthesization('(('), False)


if __name__ == "__main__":
    unittest.main()
