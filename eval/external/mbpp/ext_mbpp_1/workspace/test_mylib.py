"""MBPP-derived tests for ext_mbpp_1."""

import unittest

from mylib import similar_elements


class TestMyLib(unittest.TestCase):
    def test_case_01(self):
        assert set(similar_elements((3, 4, 5, 6),(5, 7, 4, 10))) == set((4, 5))

    def test_case_02(self):
        assert set(similar_elements((1, 2, 3, 4),(5, 4, 3, 7))) == set((3, 4))

    def test_case_03(self):
        assert set(similar_elements((11, 12, 14, 13),(17, 15, 14, 13))) == set((13, 14))


if __name__ == "__main__":
    unittest.main()
