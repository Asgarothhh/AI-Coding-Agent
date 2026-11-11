"""MBPP-derived tests for ext_mbpp_14."""

import unittest

from mylib import test_duplicate


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert test_duplicate(([1,2,3,4,5]))==False

    def test_case_02(self):
        assert test_duplicate(([1,2,3,4, 4]))==True

    def test_case_03(self):
        assert test_duplicate([1,1,2,2,3,3,4,4,5])==True


if __name__ == "__main__":
    unittest.main()
