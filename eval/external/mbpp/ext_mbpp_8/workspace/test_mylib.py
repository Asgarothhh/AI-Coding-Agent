"""MBPP-derived tests for ext_mbpp_8."""

import unittest

from mylib import remove_Occ


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert remove_Occ("hello","l") == "heo"

    def test_case_02(self):
        assert remove_Occ("abcda","a") == "bcd"

    def test_case_03(self):
        assert remove_Occ("PHP","P") == "H"


if __name__ == "__main__":
    unittest.main()
