"""MBPP-derived tests for ext_mbpp_10."""

import unittest

from mylib import find_Volume


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert find_Volume(10,8,6) == 240

    def test_case_02(self):
        assert find_Volume(3,2,2) == 6

    def test_case_03(self):
        assert find_Volume(1,2,1) == 1


if __name__ == "__main__":
    unittest.main()
