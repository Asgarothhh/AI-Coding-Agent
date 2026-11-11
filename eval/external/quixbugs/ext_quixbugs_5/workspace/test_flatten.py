"""QuixBugs-derived tests for flatten."""

import unittest

from flatten import flatten


class TestFlatten(unittest.TestCase):
    def test_case_01(self):
        self.assertEqual(flatten([[1, [], [2, 3]], [[4]], 5]), [1, 2, 3, 4, 5])

    def test_case_02(self):
        self.assertEqual(flatten([[], [], [], [], []]), [])

    def test_case_03(self):
        self.assertEqual(flatten([[], [], 1, [], 1, [], []]), [1, 1])

    def test_case_04(self):
        self.assertEqual(flatten([1, 2, 3, [[4]]]), [1, 2, 3, 4])

    def test_case_05(self):
        self.assertEqual(flatten([1, 4, 6]), [1, 4, 6])

    def test_case_06(self):
        self.assertEqual(flatten(['moe', 'curly', 'larry']), ['moe', 'curly', 'larry'])

    def test_case_07(self):
        self.assertEqual(flatten(['a', 'b', ['c'], ['d'], [['e']]]), ['a', 'b', 'c', 'd', 'e'])


if __name__ == "__main__":
    unittest.main()
