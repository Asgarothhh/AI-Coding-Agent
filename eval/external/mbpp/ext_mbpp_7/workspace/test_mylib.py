"""MBPP-derived tests for ext_mbpp_7."""

import unittest

from mylib import find_Rotations


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert find_Rotations("aaaa") == 1

    def test_case_02(self):
        assert find_Rotations("ab") == 2

    def test_case_03(self):
        assert find_Rotations("abc") == 3


if __name__ == "__main__":
    unittest.main()
