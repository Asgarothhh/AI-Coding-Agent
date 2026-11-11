"""MBPP-derived tests for ext_mbpp_2."""

import unittest

from mylib import is_not_prime


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert is_not_prime(2) == False

    def test_case_02(self):
        assert is_not_prime(10) == True

    def test_case_03(self):
        assert is_not_prime(35) == True

    def test_case_04(self):
        assert is_not_prime(37) == False


if __name__ == "__main__":
    unittest.main()
