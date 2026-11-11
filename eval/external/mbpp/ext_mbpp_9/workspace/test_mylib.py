"""MBPP-derived tests for ext_mbpp_9."""

import unittest

from mylib import sort_matrix


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert sort_matrix([[1, 2, 3], [2, 4, 5], [1, 1, 1]])==[[1, 1, 1], [1, 2, 3], [2, 4, 5]]

    def test_case_02(self):
        assert sort_matrix([[1, 2, 3], [-2, 4, -5], [1, -1, 1]])==[[-2, 4, -5], [1, -1, 1], [1, 2, 3]]

    def test_case_03(self):
        assert sort_matrix([[5,8,9],[6,4,3],[2,1,4]])==[[2, 1, 4], [6, 4, 3], [5, 8, 9]]


if __name__ == "__main__":
    unittest.main()
