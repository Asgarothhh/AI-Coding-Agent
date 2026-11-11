"""MBPP-derived tests for ext_mbpp_4."""

import unittest

from mylib import differ_At_One_Bit_Pos


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert differ_At_One_Bit_Pos(13,9) == True

    def test_case_02(self):
        assert differ_At_One_Bit_Pos(15,8) == False

    def test_case_03(self):
        assert differ_At_One_Bit_Pos(2,4) == False

    def test_case_04(self):
        assert differ_At_One_Bit_Pos(2, 3) == True

    def test_case_05(self):
        assert differ_At_One_Bit_Pos(5, 1) == True

    def test_case_06(self):
        assert differ_At_One_Bit_Pos(1, 5) == True


if __name__ == "__main__":
    unittest.main()
