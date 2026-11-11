"""MBPP-derived tests for ext_mbpp_6."""

import unittest

from mylib import square_nums


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert square_nums([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])==[1, 4, 9, 16, 25, 36, 49, 64, 81, 100]

    def test_case_02(self):
        assert square_nums([10,20,30])==([100,400,900])

    def test_case_03(self):
        assert square_nums([12,15])==([144,225])


if __name__ == "__main__":
    unittest.main()
