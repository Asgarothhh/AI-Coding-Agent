"""MBPP-derived tests for ext_mbpp_12."""

import unittest

from mylib import square_perimeter


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert square_perimeter(10)==40

    def test_case_02(self):
        assert square_perimeter(5)==20

    def test_case_03(self):
        assert square_perimeter(4)==16


if __name__ == "__main__":
    unittest.main()
