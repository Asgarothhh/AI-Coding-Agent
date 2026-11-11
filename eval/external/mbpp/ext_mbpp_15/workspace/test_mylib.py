"""MBPP-derived tests for ext_mbpp_15."""

import unittest

from mylib import is_woodall


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert is_woodall(383) == True

    def test_case_02(self):
        assert is_woodall(254) == False

    def test_case_03(self):
        assert is_woodall(200) == False


if __name__ == "__main__":
    unittest.main()
